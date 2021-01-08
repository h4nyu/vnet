import numpy as np
import typing as t
import torch
import torch.nn.functional as F
import math
import torchvision

from functools import partial
from object_detection.entities import (
    Image,
    ImageId,
    Confidences,
    PredictionSample,
    Labels,
    PascalBoxes,
    yolo_to_pascal,
    ImageBatch,
    yolo_clamp,
)
from object_detection.model_loader import ModelLoader
from object_detection.meters import MeanMeter
from object_detection.utils import DetectionPlot
from .losses import DIoU, HuberLoss, DIoULoss
from .modules import FReLU, ConvBR2d
from .atss import ATSS
from typing import Any, List, Tuple, NewType, Callable
from torchvision.ops.boxes import box_iou
from torch.utils.data import DataLoader
from torchvision.ops import nms
from torch import nn, Tensor
from itertools import product as product
from logging import getLogger
from pathlib import Path
from typing_extensions import Literal
from tqdm import tqdm
from torch.cuda.amp import GradScaler, autocast

from .bottlenecks import SENextBottleneck2d
from .bifpn import BiFPN, FP
from .losses import FocalLoss
from .anchors import Anchors
from .tta import VFlipTTA, HFlipTTA

logger = getLogger(__name__)

TrainSample = Tuple[ImageId, Image, PascalBoxes, Labels]


def collate_fn(
    batch: List[TrainSample],
) -> Tuple[ImageBatch, List[PascalBoxes], List[Labels], List[ImageId]]:
    images: List[t.Any] = []
    id_batch: List[ImageId] = []
    box_batch: List[PascalBoxes] = []
    label_batch: List[Labels] = []
    for id, img, boxes, labels in batch:
        c, h, w = img.shape
        images.append(img)
        box_batch.append(boxes)
        id_batch.append(id)
        label_batch.append(labels)
    return (
        ImageBatch(torch.stack(images)),
        box_batch,
        label_batch,
        id_batch,
    )


def prediction_collate_fn(
    batch: List[PredictionSample],
) -> Tuple[ImageBatch, List[ImageId]]:
    images: List[Any] = []
    id_batch: List[ImageId] = []
    for id, img in batch:
        images.append(img)
        id_batch.append(id)
    return ImageBatch(torch.stack(images)), id_batch


class Visualize:
    def __init__(
        self,
        out_dir: str,
        prefix: str,
        limit: int = 1,
        use_alpha: bool = True,
        show_confidences: bool = True,
    ) -> None:
        self.prefix = prefix
        self.out_dir = Path(out_dir)
        self.limit = limit
        self.use_alpha = use_alpha
        self.show_confidences = show_confidences

    def __call__(
        self,
        image_batch: ImageBatch,
        src: Tuple[List[PascalBoxes], List[Confidences], List[Labels]],
        tgt: Tuple[List[PascalBoxes], List[Labels]],
    ) -> None:
        image_batch = ImageBatch(image_batch[: self.limit])
        gt_boxes, gt_labels = tgt
        gt_boxes = gt_boxes[: self.limit]
        gt_labels = gt_labels[: self.limit]
        box_batch, confidence_batch, label_batch = src
        _, _, h, w = image_batch.shape
        for i, (img, boxes, confidences, labels, gtb, gtl,) in enumerate(
            zip(
                image_batch,
                box_batch,
                confidence_batch,
                label_batch,
                gt_boxes,
                gt_labels,
            )
        ):
            plot = DetectionPlot(img)
            plot.draw_boxes(boxes=gtb, color="blue", labels=gtl)
            plot.draw_boxes(
                boxes=boxes, color="red", labels=labels, confidences=confidences
            )
            plot.save(f"{self.out_dir}/{self.prefix}-boxes-{i}.png")


class ClassificationModel(nn.Module):
    def __init__(
        self,
        in_channels: int,
        depth: int,
        num_anchors: int = 9,
        num_classes: int = 80,
    ) -> None:
        super(ClassificationModel, self).__init__()
        self.num_classes = num_classes
        self.num_anchors = num_anchors
        self.conv = nn.Sequential(
            *[
                nn.Sequential(
                    ConvBR2d(in_channels, in_channels),
                    FReLU(in_channels),
                )
                for _ in range(depth)
            ]
        )
        self.output = nn.Conv2d(
            in_channels,
            num_anchors * num_classes,
            kernel_size=1,
            padding=0,
        )

    def forward(self, x: Tensor) -> Tensor:
        x = self.conv(x)
        x = self.output(x)
        out = x.permute(0, 2, 3, 1)
        batch_size, width, height, channels = out.shape
        out = out.view(
            batch_size,
            width,
            height,
            self.num_anchors,
            self.num_classes,
        )
        return out.contiguous().view(batch_size, -1, self.num_classes).sigmoid()


class RegressionModel(nn.Module):
    def __init__(
        self,
        in_channels: int,
        num_anchors: int,
        depth: int = 1,
    ) -> None:
        super().__init__()
        self.conv = nn.Sequential(
            *[
                nn.Sequential(
                    ConvBR2d(in_channels, in_channels),
                    FReLU(in_channels),
                )
                for _ in range(depth)
            ]
        )
        self.out = nn.Conv2d(
            in_channels,
            num_anchors * 4,
            kernel_size=1,
            padding=0,
        )

    def forward(self, x: Tensor) -> Tensor:
        x = self.conv(x)
        x = self.out(x)
        x = x.permute(0, 2, 3, 1)
        x = x.contiguous().view(x.shape[0], -1, 4)
        return x


BoxDiff = NewType("BoxDiff", Tensor)
BoxDiffs = NewType("BoxDiffs", Tensor)

NetOutput = Tuple[List[PascalBoxes], List[BoxDiffs], List[Tensor]]


def _init_weight(m: nn.Module) -> None:
    """Weight initialization as per Tensorflow official implementations."""
    if isinstance(m, nn.BatchNorm2d):
        # looks like all bn init the same?
        m.weight.data.fill_(1.0)
        m.bias.data.zero_()


class EfficientDet(nn.Module):
    def __init__(
        self,
        num_classes: int,
        backbone: nn.Module,
        channels: int = 64,
        out_ids: List[int] = [6, 7],
        anchors: Anchors = Anchors(),
        fpn_depth: int = 1,
        box_depth: int = 1,
        cls_depth: int = 1,
    ) -> None:
        super().__init__()
        self.out_ids = np.array(out_ids) - 3
        self.anchors = anchors
        self.backbone = backbone
        self.neck = nn.Sequential(*[BiFPN(channels=channels) for _ in range(fpn_depth)])
        self.box_reg = RegressionModel(
            depth=box_depth,
            in_channels=channels,
            num_anchors=self.anchors.num_anchors,
        )
        self.classification = ClassificationModel(
            channels,
            depth=cls_depth,
            num_classes=num_classes,
            num_anchors=self.anchors.num_anchors,
        )
        for n, m in self.named_modules():
            if "backbone" not in n:
                _init_weight(m)

    def forward(self, images: ImageBatch) -> NetOutput:
        features = self.backbone(images)
        features = self.neck(features)
        anchor_levels = [self.anchors(features[i], 2 ** (i + 1)) for i in self.out_ids]
        box_levels = [self.box_reg(features[i]) for i in self.out_ids]
        label_levels = [self.classification(features[i]) for i in self.out_ids]
        return (
            anchor_levels,
            box_levels,
            label_levels,
        )


class Criterion:
    def __init__(
        self,
        num_classes: int = 1,
        topk: int = 13,
        box_weight: float = 3.0,
        cls_weight: float = 1.0,
    ) -> None:
        self.num_classes = num_classes
        self.box_weight = box_weight
        self.cls_weight = cls_weight
        self.box_loss = DIoULoss(size_average=True)
        self.atss = ATSS(topk)
        self.cls_loss = FocalLoss()

    def __call__(
        self,
        images: ImageBatch,
        net_output: NetOutput,
        gt_boxes_list: List[PascalBoxes],
        gt_classes_list: List[Labels],
    ) -> Tuple[Tensor, Tensor, Tensor]:
        (
            anchor_levels,
            box_reg_levels,
            cls_pred_levels,
        ) = net_output
        device = anchor_levels[0].device
        batch_size = box_reg_levels[0].shape[0]
        _, _, h, w = images.shape
        anchors = PascalBoxes(torch.cat([*anchor_levels], dim=0))
        box_preds = torch.cat([*box_reg_levels], dim=1)
        cls_preds = torch.cat([*cls_pred_levels], dim=1)

        box_losses = torch.zeros(batch_size, device=device)
        cls_losses = torch.zeros(batch_size, device=device)
        for batch_id, (gt_boxes, gt_lables, box_pred, cls_pred,) in enumerate(
            zip(
                gt_boxes_list,
                gt_classes_list,
                box_preds,
                cls_preds,
            )
        ):
            pos_ids = self.atss(
                anchors,
                PascalBoxes(gt_boxes),
            )
            matched_gt_boxes = gt_boxes[pos_ids[:, 0]]
            matched_pred_boxes = anchors[pos_ids[:, 1]] + box_pred[pos_ids[:, 1]]
            cls_target = torch.zeros(cls_pred.shape, device=device)
            cls_target[pos_ids[:, 1], gt_lables[pos_ids[:, 0]].long()] = 1
            cls_losses[batch_id] = self.cls_loss(
                cls_pred.float(),
                cls_target.float(),
            ).sum()

            if len(gt_boxes) == 0:
                continue
            box_losses[batch_id] = self.box_loss(
                PascalBoxes(matched_gt_boxes),
                PascalBoxes(matched_pred_boxes),
            ).mean()

        box_loss = box_losses.mean() * self.box_weight
        cls_loss = cls_losses.mean() * self.cls_weight
        loss = box_loss + cls_loss
        return loss, box_loss, cls_loss


class PreProcess:
    def __init__(self, device: t.Any, non_blocking: bool = True) -> None:
        super().__init__()
        self.device = device
        self.non_blocking = non_blocking

    def __call__(
        self,
        batch: t.Tuple[ImageBatch, List[PascalBoxes], List[Labels]],
    ) -> t.Tuple[ImageBatch, List[PascalBoxes], List[Labels]]:
        image_batch, boxes_batch, label_batch = batch
        return (
            ImageBatch(
                image_batch.to(
                    self.device,
                    non_blocking=self.non_blocking,
                )
            ),
            [
                PascalBoxes(
                    x.to(
                        self.device,
                        non_blocking=self.non_blocking,
                    )
                )
                for x in boxes_batch
            ],
            [
                Labels(
                    x.to(
                        self.device,
                        non_blocking=self.non_blocking,
                    )
                )
                for x in label_batch
            ],
        )


class ToBoxes:
    def __init__(
        self,
        confidence_threshold: float = 0.5,
        iou_threshold: float = 0.5,
    ) -> None:
        self.confidence_threshold = confidence_threshold
        self.iou_threshold = iou_threshold

    @torch.no_grad()
    def __call__(
        self, net_output: NetOutput
    ) -> t.Tuple[List[PascalBoxes], List[Confidences], List[Labels]]:
        (
            anchor_levels,
            box_diff_levels,
            labels_levels,
        ) = net_output
        box_batch = []
        confidence_batch = []
        label_batch = []
        anchors = torch.cat(anchor_levels, dim=0)  # type: ignore
        box_diffs = torch.cat(box_diff_levels, dim=1)  # type:ignore
        labels_batch = torch.cat(labels_levels, dim=1)  # type:ignore
        for box_diff, preds in zip(box_diffs, labels_batch):
            boxes = anchors + box_diff
            confidences, c_index = preds.max(dim=1)
            filter_idx = confidences > self.confidence_threshold
            confidences = confidences[filter_idx]
            c_index = c_index[filter_idx]
            boxes = boxes[filter_idx]
            sort_idx = nms(
                boxes,
                confidences,
                self.iou_threshold,
            )
            confidences.argsort(descending=True)
            boxes = PascalBoxes(boxes[sort_idx])
            confidences = confidences[sort_idx]
            c_index = c_index[sort_idx]
            box_batch.append(boxes)
            confidence_batch.append(Confidences(confidences))
            label_batch.append(Labels(c_index))
        return box_batch, confidence_batch, label_batch


class Trainer:
    def __init__(
        self,
        model: nn.Module,
        train_loader: DataLoader,
        test_loader: DataLoader,
        model_loader: ModelLoader,
        visualize: Visualize,
        optimizer: t.Any,
        get_score: Callable[[PascalBoxes, PascalBoxes], float],
        to_boxes: ToBoxes,
        device: str = "cpu",
        use_amp: bool = False,
        criterion: Criterion = Criterion(),
    ) -> None:
        self.device = torch.device(device)
        self.model_loader = model_loader
        self.model = model.to(self.device)
        self.use_amp = use_amp
        self.preprocess = PreProcess(self.device)
        self.to_boxes = to_boxes
        self.optimizer = optimizer
        self.train_loader = train_loader
        self.test_loader = test_loader
        self.criterion = criterion
        self.visualize = visualize
        self.get_score = get_score
        self.scaler = GradScaler()
        self.meters = {
            key: MeanMeter()
            for key in [
                "train_loss",
                "train_box",
                "train_label",
                "test_loss",
                "test_box",
                "test_label",
                "score",
            ]
        }

    def log(self) -> None:
        value = ("|").join([f"{k}:{v.get_value():.4f}" for k, v in self.meters.items()])
        logger.info(value)

    def reset_meters(self) -> None:
        for v in self.meters.values():
            v.reset()

    def __call__(self, epochs: int) -> None:
        self.model = self.model_loader.load_if_needed(self.model)
        for epoch in range(epochs):
            self.train_one_epoch()
            self.eval_one_epoch()
            self.log()
            self.reset_meters()

    def train_one_epoch(self) -> None:
        self.model.train()
        loader = self.train_loader
        for (
            samples,
            gt_boxes_list,
            gt_cls_list,
            ids,
        ) in tqdm(loader):
            (
                samples,
                gt_boxes_list,
                gt_cls_list,
            ) = self.preprocess((samples, gt_boxes_list, gt_cls_list))

            self.optimizer.zero_grad()
            with autocast(enabled=self.use_amp):
                outputs = self.model(samples)
                loss, box_loss, label_loss = self.criterion(
                    samples,
                    outputs,
                    gt_boxes_list,
                    gt_cls_list,
                )
            self.scaler.scale(loss).backward()
            self.scaler.step(self.optimizer)
            self.scaler.update()
            self.meters["train_loss"].update(loss.item())
            self.meters["train_box"].update(box_loss.item())
            self.meters["train_label"].update(label_loss.item())

    @torch.no_grad()
    def eval_one_epoch(self) -> None:
        self.model.eval()
        loader = self.test_loader
        for samples, box_batch, gt_cls_list, ids in tqdm(loader):
            (
                samples,
                box_batch,
                gt_cls_list,
            ) = self.preprocess((samples, box_batch, gt_cls_list))
            outputs = self.model(samples)
            loss, box_loss, label_loss = self.criterion(
                samples, outputs, box_batch, gt_cls_list
            )
            self.meters["test_loss"].update(loss.item())
            self.meters["test_box"].update(box_loss.item())
            self.meters["test_label"].update(label_loss.item())

            preds = self.to_boxes(outputs)
            for (pred, gt) in zip(preds[0], box_batch):
                self.meters["score"].update(self.get_score(pred, gt))

        self.visualize(
            samples,
            preds,
            (box_batch, gt_cls_list),
        )
        self.model_loader.save_if_needed(
            self.model,
            self.meters[self.model_loader.key].get_value(),
        )

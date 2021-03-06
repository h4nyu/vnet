import torch, math, numpy as np
from typing import *
import torch.nn.functional as F
from functools import partial
from typing import (
    NewType,
    Union,
    Callable,
    Any,
)
from torch import nn, Tensor
from typing_extensions import Literal
from logging import getLogger
from tqdm import tqdm
from vnet import (
    BoxMap,
    BoxMaps,
    YoloBoxes,
    Confidences,
    Boxes,
    yolo_to_pascal,
    pascal_to_yolo,
    yolo_to_coco,
    Labels,
    boxmap_to_boxes,
    ImageBatch,
    PredBoxes,
    Image,
    resize_points,
)
from vnet.point import Points
from vnet.utils import DetectionPlot
from .mkmaps import Heatmaps, MkMapsFn, MkBoxMapsFn
from .modules import (
    FReLU,
    ConvBR2d,
    SeparableConv2d,
    SeparableConvBR2d,
    MemoryEfficientSwish,
)
from .bottlenecks import SENextBottleneck2d
from .bifpn import BiFPN, FP
from .losses import HuberLoss, DIoULoss
from .anchors import EmptyAnchors
from .matcher import NearnestMatcher, CenterMatcher
from vnet.meters import MeanMeter
from torch.cuda.amp import GradScaler, autocast
from torchvision.ops import nms
from torch.utils.data import DataLoader
from vnet.model_loader import ModelLoader

from pathlib import Path

logger = getLogger(__name__)


class Head(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        depth: int,
    ) -> None:
        super().__init__()
        channels = in_channels
        self.conv = nn.Sequential(
            *[
                nn.Sequential(
                    SeparableConvBR2d(in_channels, in_channels),
                    MemoryEfficientSwish(),
                )
                for _ in range(depth)
            ]
        )

        self.out = nn.Sequential(
            SeparableConv2d(
                in_channels,
                out_channels,
            )
        )

    def forward(self, x: Tensor) -> Tensor:
        x = self.conv(x)
        x = self.out(x)
        return x


NetOutput = Tuple[Heatmaps, BoxMaps, BoxMap]  # label, pos, size, count


class CenterNet(nn.Module):
    def __init__(
        self,
        channels: int,
        num_classes: int,
        backbone: nn.Module,
        box_depth: int = 1,
        cls_depth: int = 1,
        fpn_depth: int = 1,
        out_idx: int = 4,
    ) -> None:
        super().__init__()
        self.out_idx = out_idx - 3
        self.channels = channels
        self.backbone = backbone
        self.fpn = nn.Sequential(*[BiFPN(channels=channels) for _ in range(fpn_depth)])
        self.hm_reg = nn.Sequential(
            Head(
                in_channels=channels,
                out_channels=num_classes,
                depth=cls_depth,
            ),
            nn.Sigmoid(),
        )
        self.box_reg = nn.Sequential(
            Head(
                in_channels=channels,
                out_channels=4,
                depth=box_depth,
            )
        )
        self.anchors = EmptyAnchors()

    def forward(self, x: ImageBatch) -> NetOutput:
        fp = self.backbone(x)
        fp = self.fpn(fp)
        heatmaps = Heatmaps(self.hm_reg(fp[self.out_idx]))
        anchors = self.anchors(heatmaps)
        boxmaps = self.box_reg(fp[self.out_idx])
        return (heatmaps, BoxMaps(boxmaps), anchors)


class HMLoss(nn.Module):
    """
    Modified focal loss
    """

    def __init__(
        self,
        alpha: float = 2.0,
        beta: float = 2.0,
        eps: float = 5e-4,
    ):
        super().__init__()
        self.alpha = alpha
        self.beta = beta
        self.eps = eps

    def forward(self, pred: Tensor, gt: Tensor) -> Tensor:
        """
        pred: 0-1 [B, C,..]
        gt: 0-1 [B, C,..]
        """
        alpha = self.alpha
        beta = self.beta
        eps = self.eps
        pred = torch.clamp(pred, min=self.eps, max=1 - self.eps)
        pos_mask = gt.eq(1).float()
        neg_mask = gt.lt(1).float()
        pos_loss = -((1 - pred) ** alpha) * torch.log(pred) * pos_mask
        pos_loss = pos_loss.sum()

        neg_weight = (1 - gt) ** beta
        neg_loss = neg_weight * (-(pred ** alpha) * torch.log(1 - pred) * neg_mask)
        neg_loss = neg_loss.sum()
        loss = (pos_loss + neg_loss) / pos_mask.sum().clamp(min=1.0)
        return loss


class Criterion:
    def __init__(
        self,
        mk_hmmaps: MkMapsFn,
        mk_boxmaps: MkBoxMapsFn,
        heatmap_weight: float = 1.0,
        box_weight: float = 1.0,
        count_weight: float = 1.0,
        sigma: float = 0.3,
    ) -> None:
        super().__init__()
        self.hmloss = HMLoss()
        self.boxloss = BoxLoss()
        self.heatmap_weight = heatmap_weight
        self.box_weight = box_weight
        self.count_weight = count_weight
        self.mk_hmmaps = mk_hmmaps

    def __call__(
        self,
        images: ImageBatch,
        netout: NetOutput,
        gt_box_batch: List[YoloBoxes],
        gt_label_batch: List[Labels],
    ) -> Tuple[Tensor, Tensor, Tensor, Heatmaps]:
        s_hm, s_bm, anchors = netout
        _, _, orig_h, orig_w = images.shape
        _, _, h, w = s_hm.shape
        t_hm = self.mk_hmmaps(gt_box_batch, gt_label_batch, (h, w), (orig_h, orig_w))
        hm_loss = self.hmloss(s_hm, t_hm) * self.heatmap_weight
        box_loss = self.boxloss(s_bm, gt_box_batch, anchors) * self.box_weight
        loss = hm_loss + box_loss
        return (loss, hm_loss, box_loss, t_hm)


class BoxLoss:
    def __init__(
        self,
        matcher: Any = NearnestMatcher(),
        use_diff: bool = True,
    ) -> None:
        self.matcher = matcher
        self.loss = DIoULoss(size_average=True)
        self.use_diff = use_diff

    def __call__(
        self,
        preds: BoxMaps,
        gt_box_batch: List[YoloBoxes],
        anchormap: BoxMap,
    ) -> Tensor:
        device = preds.device
        _, _, h, w = preds.shape
        box_losses: List[Tensor] = []
        anchors = boxmap_to_boxes(anchormap)
        for diff_map, gt_boxes in zip(preds, gt_box_batch):
            if len(gt_boxes) == 0:
                continue

            pred_boxes = boxmap_to_boxes(BoxMap(diff_map))
            match_indices, positive_indices = self.matcher(anchors, gt_boxes, (w, h))
            num_pos = positive_indices.sum()
            if num_pos == 0:
                continue
            matched_gt_boxes = YoloBoxes(gt_boxes[match_indices][positive_indices])
            matched_pred_boxes = YoloBoxes(pred_boxes[positive_indices])
            if self.use_diff:
                matched_pred_boxes = YoloBoxes(
                    anchors[positive_indices] + matched_pred_boxes
                )
            box_losses.append(
                self.loss(
                    yolo_to_pascal(matched_pred_boxes, (1, 1)),
                    yolo_to_pascal(matched_gt_boxes, (1, 1)),
                )
            )
        if len(box_losses) == 0:
            return torch.tensor(0.0).to(device)
        return torch.stack(box_losses).mean()


class ToBoxes:
    def __init__(
        self,
        threshold: float = 0.1,
        iou_threshold: float = 0.5,
        kernel_size: int = 3,
        limit: int = 100,
        use_diff: bool = True,
    ) -> None:
        self.limit = limit
        self.threshold = threshold
        self.kernel_size = kernel_size
        self.iou_threshold = iou_threshold
        self.use_diff = use_diff
        self.max_pool = partial(
            F.max_pool2d,
            kernel_size=kernel_size,
            padding=kernel_size // 2,
            stride=1,
        )

    @torch.no_grad()
    def __call__(
        self, inputs: NetOutput
    ) -> Tuple[List[YoloBoxes], List[Confidences], List[Labels]]:
        heatmaps, boxmaps, anchormap = inputs
        device = heatmaps.device
        kpmaps = heatmaps * (
            (self.max_pool(heatmaps) == heatmaps) & (heatmaps > self.threshold)
        )
        kpmaps, labelmaps = torch.max(kpmaps, dim=1)
        box_batch: List[YoloBoxes] = []
        confidence_batch: List[Confidences] = []
        label_batch: List[Labels] = []
        for km, lm, bm in zip(kpmaps, labelmaps, boxmaps):
            kp = torch.nonzero(km, as_tuple=False)  # type: ignore
            pos_idx = (kp[:, 0], kp[:, 1])
            confidences = km[pos_idx]
            labels = lm[pos_idx]
            if self.use_diff:
                boxes = (
                    anchormap[:, pos_idx[0], pos_idx[1]].t()
                    + bm[:, pos_idx[0], pos_idx[1]].t()
                )
            else:
                boxes = bm[:, pos_idx[0], pos_idx[1]].t()

            unique_labels = labels.unique()
            box_List: List[Tensor] = []
            confidence_List: List[Tensor] = []
            label_List: List[Tensor] = []

            for c in unique_labels:
                cls_indices = labels == c
                if cls_indices.sum() == 0:
                    continue

                c_boxes = boxes[cls_indices]
                c_confidences = confidences[cls_indices]
                c_labels = labels[cls_indices]
                nms_indices = nms(
                    yolo_to_pascal(c_boxes, (1, 1)),
                    c_confidences,
                    self.iou_threshold,
                )[: self.limit]
                box_List.append(c_boxes[nms_indices])
                confidence_List.append(c_confidences[nms_indices])
                label_List.append(c_labels[nms_indices])

            if len(confidence_List) > 0:
                confidences = torch.cat(confidence_List, dim=0)
            else:
                confidences = torch.zeros(
                    0, device=confidences.device, dtype=confidences.dtype
                )
            if len(box_List) > 0:
                boxes = torch.cat(box_List, dim=0)
            else:
                boxes = torch.zeros(0, device=boxes.device, dtype=boxes.dtype)
            if len(label_List) > 0:
                labels = torch.cat(label_List, dim=0)
            else:
                labels = torch.zeros(0, device=labels.device, dtype=labels.dtype)

            sort_indices = confidences.argsort(descending=True)
            boxes = boxes[sort_indices]
            confidences = confidences[sort_indices]
            labels = labels[sort_indices]
            box_batch.append(YoloBoxes(boxes))
            confidence_batch.append(Confidences(confidences))
            label_batch.append(Labels(labels))
        return box_batch, confidence_batch, label_batch


class ToPoints:
    def __init__(
        self,
        threshold: float = 0.1,
        iou_threshold: float = 0.5,
        kernel_size: int = 3,
        limit: int = 100,
        use_diff: bool = True,
    ) -> None:
        self.limit = limit
        self.threshold = threshold
        self.kernel_size = kernel_size
        self.iou_threshold = iou_threshold
        self.use_diff = use_diff
        self.max_pool = partial(
            F.max_pool2d,
            kernel_size=kernel_size,
            padding=kernel_size // 2,
            stride=1,
        )

    @torch.no_grad()
    def __call__(
        self,
        heatmaps: Heatmaps,
        w: int,
        h: int,
    ) -> Tuple[List[Points], List[Confidences], List[Labels]]:
        device = heatmaps.device
        kpmaps = heatmaps * (
            (self.max_pool(heatmaps) == heatmaps) & (heatmaps > self.threshold)
        )
        kpmaps, labelmaps = torch.max(kpmaps, dim=1)
        point_batch: List[Points] = []
        confidence_batch: List[Confidences] = []
        label_batch: List[Labels] = []
        _, _, hm_h, hm_w = heatmaps.shape
        for km, lm in zip(kpmaps, labelmaps):
            kp = torch.nonzero(km, as_tuple=False)  # type: ignore
            pos_idx = (kp[:, 0], kp[:, 1])
            confidences = km[pos_idx]
            labels = lm[pos_idx]
            points: Tensor = resize_points(
                Points(torch.stack([kp[:, 1], kp[:, 0]], dim=-1)),
                scale_x=1 / hm_w,
                scale_y=1 / hm_h,
            )
            unique_labels = labels.unique()
            point_List: List[Tensor] = []
            confidence_List: List[Tensor] = []
            label_List: List[Tensor] = []

            for c in unique_labels:
                cls_indices = labels == c
                if cls_indices.sum() == 0:
                    continue
                c_points = points[cls_indices]
                c_confidences = confidences[cls_indices]
                c_labels = labels[cls_indices]
                c_sort_indices = c_confidences.argsort(descending=True)
                point_List.append(c_points[c_sort_indices])
                confidence_List.append(c_confidences[c_sort_indices])
                label_List.append(c_labels[c_sort_indices])

            if len(confidence_List) > 0:
                confidences = torch.cat(confidence_List, dim=0)
            else:
                confidences = torch.zeros(
                    0, device=confidences.device, dtype=confidences.dtype
                )
            if len(point_List) > 0:
                points = torch.cat(point_List, dim=0)
            else:
                points = torch.zeros(0, device=points.device, dtype=points.dtype)
            if len(label_List) > 0:
                labels = torch.cat(label_List, dim=0)
            else:
                labels = torch.zeros(0, device=labels.device, dtype=labels.dtype)

            sort_indices = confidences.argsort(descending=True)
            points = points[sort_indices]
            confidences = confidences[sort_indices]
            labels = labels[sort_indices]
            point_batch.append(Points(points))
            confidence_batch.append(Confidences(confidences))
            label_batch.append(Labels(labels))
        return point_batch, confidence_batch, label_batch


class Visualize:
    def __init__(
        self,
        out_dir: str,
        prefix: str,
        limit: int = 1,
        use_alpha: bool = True,
        show_confidences: bool = True,
        figsize: Tuple[int, int] = (10, 10),
        transforms: Any = None,
    ) -> None:
        self.prefix = prefix
        self.out_dir = Path(out_dir)
        self.limit = limit
        self.use_alpha = use_alpha
        self.show_confidences = show_confidences
        self.figsize = figsize
        self.transforms = transforms

    @torch.no_grad()
    def __call__(
        self,
        net_out: NetOutput,
        box_batch: List[YoloBoxes],
        confidence_batch: List[Confidences],
        label_batch: List[Labels],
        gt_box_batch: List[YoloBoxes],
        gt_label_batch: List[Labels],
        image_batch: ImageBatch,
        gt_hms: Heatmaps,
    ) -> None:
        heatmap, _, _ = net_out
        box_batch = box_batch[: self.limit]
        confidence_batch = confidence_batch[: self.limit]
        label_batch = label_batch[: self.limit]
        gt_box_batch = gt_box_batch[: self.limit]
        gt_label_batch = gt_label_batch[: self.limit]
        _, _, h, w = image_batch.shape
        for i, (
            boxes,
            confidences,
            labels,
            gt_boxes,
            gt_labels,
            hm,
            img,
            gt_hm,
        ) in enumerate(
            zip(
                box_batch,
                confidence_batch,
                label_batch,
                gt_box_batch,
                gt_label_batch,
                heatmap,
                image_batch,
                gt_hms,
            )
        ):
            plot = DetectionPlot(
                self.transforms(img) if self.transforms is not None else img
            )
            plot.draw_boxes(
                boxes=yolo_to_pascal(gt_boxes, (w, h)), labels=gt_labels, color="blue"
            )
            plot.draw_boxes(
                boxes=yolo_to_pascal(boxes, (w, h)),
                labels=labels,
                confidences=confidences,
                color="red",
            )
            plot.save(f"{self.out_dir}/{self.prefix}-boxes-{i}.png")
            gt_merged_hm, _ = torch.max(gt_hm, dim=0)
            plot = DetectionPlot(gt_merged_hm)
            plot.save(f"{self.out_dir}/{self.prefix}-gt-hm-{i}.png")
            merged_hm, _ = torch.max(hm, dim=0)
            plot = DetectionPlot(merged_hm)
            plot.save(f"{self.out_dir}/{self.prefix}-hm-{i}.png")

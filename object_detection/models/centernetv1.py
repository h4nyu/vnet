import torch
import torch.nn.functional as F
from functools import partial
from torch import nn, Tensor
from typing import Tuple, List, NewType, Callable, Any
from torch.utils.data import DataLoader
from typing_extensions import Literal
from pathlib import Path
from object_detection.meters import BestWatcher, MeanMeter
from object_detection.utils import DetectionPlot
from .centernet import (
    CenterNet,
    Reg,
    Sizemap,
    DiffMap,
    HMLoss,
    PreProcess,
    Labels,
    Batch,
    ImageId,
    Heatmaps,
)
from logging import getLogger
from tqdm import tqdm
from .efficientdet import Anchors, RegressionModel
from .bifpn import BiFPN
from torchvision.ops.boxes import box_iou
from torchvision.ops import nms
from object_detection.model_loader import ModelLoader
from object_detection.entities import (
    PyramidIdx,
    ImageBatch,
    YoloBoxes,
    Confidences,
    yolo_to_pascal,
)

logger = getLogger(__name__)


BoxDiffs = NewType("BoxDiffs", Tensor)  # [B, num_anchors, 4]
BoxDiff = NewType("BoxDiff", Tensor)  # [num_anchors, 4]
Heatmap = NewType("Heatmap", Tensor)  # [1, H, W]
NetOutput = Tuple[YoloBoxes, BoxDiffs, Heatmaps]


def collate_fn(
    batch: Batch,
) -> Tuple[ImageBatch, List[YoloBoxes], List[Labels], List[ImageId]]:
    images: List[Any] = []
    id_batch: List[ImageId] = []
    box_batch: List[YoloBoxes] = []
    label_batch: List[Labels] = []

    for id, img, boxes, labels in batch:
        images.append(img)
        box_batch.append(boxes)
        id_batch.append(id)
        label_batch.append(labels)
    return ImageBatch(torch.stack(images)), box_batch, label_batch, id_batch


class GetPeaks:
    def __init__(self, threshold: float = 0.1, kernel_size: int = 3) -> None:
        self.threshold = threshold
        self.max_pool = partial(
            F.max_pool2d, kernel_size=kernel_size, padding=kernel_size // 2, stride=1
        )

    def __call__(self, x: Heatmap) -> Tensor:
        kp = (self.max_pool(x) == x) & (x > self.threshold)
        return kp.nonzero()[:, -2:]


class Visualize:
    def __init__(
        self,
        out_dir: str,
        prefix: str,
        limit: int = 1,
        use_alpha: bool = True,
        show_probs: bool = True,
        figsize: Tuple[int, int] = (10, 10),
    ) -> None:
        self.prefix = prefix
        self.out_dir = Path(out_dir)
        self.limit = limit
        self.use_alpha = use_alpha
        self.show_probs = show_probs
        self.figsize = figsize

    @torch.no_grad()
    def __call__(
        self,
        net_out: NetOutput,
        preds: Tuple[List[YoloBoxes], List[Confidences]],
        gts: List[YoloBoxes],
        image_batch: ImageBatch,
        gt_hms: Heatmaps,
    ) -> None:
        _, _, heatmaps = net_out
        box_batch, confidence_batch = preds
        box_batch = box_batch[: self.limit]
        confidence_batch = confidence_batch[: self.limit]
        _, _, h, w = image_batch.shape
        for i, (sb, sc, tb, hm, img, gt_hm) in enumerate(
            zip(box_batch, confidence_batch, gts, heatmaps, image_batch, gt_hms)
        ):
            plot = DetectionPlot(
                h=h,
                w=w,
                use_alpha=self.use_alpha,
                figsize=self.figsize,
                show_probs=self.show_probs,
            )
            plot.with_image(img, alpha=0.7)
            plot.with_image(hm[0].log(), alpha=0.3)
            plot.with_yolo_boxes(tb, color="blue")
            plot.with_yolo_boxes(sb, sc, color="red")
            plot.save(f"{self.out_dir}/{self.prefix}-boxes-{i}.png")

            plot = DetectionPlot(
                h=h, w=w, use_alpha=self.use_alpha, figsize=self.figsize
            )
            plot.with_image(img, alpha=0.7)
            plot.with_image((gt_hm[0] + 1e-4).log(), alpha=0.3)
            plot.save(f"{self.out_dir}/{self.prefix}-hm-{i}.png")


class CenterNetV1(nn.Module):
    def __init__(
        self,
        channels: int,
        backbone: nn.Module,
        depth: int = 2,
        out_idx: PyramidIdx = 4,
        anchors: Anchors = Anchors(size=4, scales=[1.0], ratios=[1.0]),
    ) -> None:
        super().__init__()
        self.out_idx = out_idx - 3
        self.channels = channels
        self.backbone = backbone
        self.fpn = nn.Sequential(*[BiFPN(channels=channels) for _ in range(depth)])
        self.hm_reg = nn.Sequential(
            Reg(in_channels=channels, out_channels=1, depth=depth), nn.Sigmoid(),
        )
        self.anchors = anchors

        self.box_reg = RegressionModel(
            in_channels=channels,
            hidden_channels=channels,
            num_anchors=self.anchors.num_anchors,
            out_size=4,
        )

    def forward(self, x: ImageBatch) -> NetOutput:
        fp = self.backbone(x)
        fp = self.fpn(fp)
        heatmaps = self.hm_reg(fp[self.out_idx])
        anchors = self.anchors(heatmaps)
        box_diffs = self.box_reg(fp[self.out_idx])
        return anchors, BoxDiffs(box_diffs), Heatmaps(heatmaps)


class ToBoxes:
    def __init__(
        self,
        threshold: float = 0.1,
        kernel_size: int = 5,
        limit: int = 100,
        count_offset: int = 1,
        nms_threshold: float = 0.8,
    ) -> None:
        self.limit = limit
        self.threshold = threshold
        self.kernel_size = kernel_size
        self.count_offset = count_offset
        self.nms_threshold = nms_threshold
        self.max_pool = partial(
            F.max_pool2d, kernel_size=kernel_size, padding=kernel_size // 2, stride=1
        )

    @torch.no_grad()
    def __call__(self, inputs: NetOutput) -> Tuple[List[YoloBoxes], List[Confidences]]:
        anchors, box_diffs, heatmap = inputs
        device = heatmap.device
        kpmap = (self.max_pool(heatmap) == heatmap) & (heatmap > self.threshold)
        batch_size, _, height, width = heatmap.shape
        original_wh = torch.tensor([width, height], dtype=torch.float32).to(device)
        rows: List[Tuple[YoloBoxes, Confidences]] = []
        box_batch = []
        conf_batch = []
        for hm, km, box_diff in zip(heatmap.squeeze(1), kpmap.squeeze(1), box_diffs):
            kp = km.nonzero()
            confidences = hm[kp[:, 0], kp[:, 1]]
            cxcy = kp[:, [1, 0]]
            indecies = cxcy[:, 0] + cxcy[:, 1] * width
            box_diff = box_diff[indecies]
            anchor = anchors[indecies]
            boxes = anchor + box_diff
            sort_idx = nms(
                yolo_to_pascal(YoloBoxes(boxes), (1, 1)),
                confidences,
                iou_threshold=self.nms_threshold,
            )[: self.limit]
            box_batch.append(YoloBoxes(boxes[sort_idx]))
            conf_batch.append(Confidences(confidences[sort_idx]))
        return box_batch, conf_batch


class BoxLoss:
    def __init__(self, iou_threshold: float = 0.5) -> None:
        self.iou_threshold = iou_threshold
        self.get_peaks = GetPeaks()

    def __call__(
        self,
        anchors: YoloBoxes,
        box_diff: BoxDiff,
        gt_boxes: YoloBoxes,
        heatmap: Heatmap,
    ) -> Tensor:
        device = box_diff.device
        _, h, w = heatmap.shape

        kp = self.get_peaks(heatmap)
        cxcy = kp[:, [1, 0]]
        indecies = cxcy[:, 0] + cxcy[:, 1] * w
        anchors = YoloBoxes(anchors[indecies])
        box_diff = BoxDiff(box_diff[indecies])

        iou_matrix = box_iou(
            yolo_to_pascal(anchors, wh=(1, 1)), yolo_to_pascal(gt_boxes, wh=(1, 1)),
        )
        iou_max, match_indices = torch.max(iou_matrix, dim=1)
        positive_indices = iou_max > self.iou_threshold
        num_pos = positive_indices.sum()

        if num_pos == 0:
            return torch.tensor(0.0).to(device)
        matched_gt_boxes = gt_boxes[match_indices][positive_indices]
        matched_anchors = anchors[positive_indices]
        pred_diff = box_diff[positive_indices]
        gt_diff = matched_gt_boxes - matched_anchors
        return F.l1_loss(pred_diff, gt_diff, reduction="mean")


class MkMaps:
    def __init__(
        self,
        sigma: float = 0.5,
        mode: Literal["length", "aspect", "constant"] = "length",
    ) -> None:
        self.sigma = sigma
        self.mode = mode

    def _mkmaps(
        self, boxes: YoloBoxes, hw: Tuple[int, int], original_hw: Tuple[int, int]
    ) -> Heatmaps:
        device = boxes.device
        h, w = hw
        orig_h, orig_w = original_hw
        heatmap = torch.zeros((1, 1, h, w), dtype=torch.float32).to(device)
        box_count = len(boxes)
        counts = torch.tensor([box_count]).to(device)
        if box_count == 0:
            return Heatmaps(heatmap)

        box_cxs, box_cys, _, _ = boxes.unbind(-1)
        grid_y, grid_x = torch.meshgrid(  # type:ignore
            torch.arange(h, dtype=torch.int64), torch.arange(w, dtype=torch.int64),
        )
        wh = torch.tensor([w, h]).to(device)
        cxcy = boxes[:, :2] * wh
        cx = cxcy[:, 0]
        cy = cxcy[:, 1]
        grid_xy = torch.stack([grid_x, grid_y]).to(device).expand((box_count, 2, h, w))
        grid_cxcy = cxcy.view(box_count, 2, 1, 1).expand_as(grid_xy)
        if self.mode == "aspect":
            weight = (boxes[:, 2:] ** 2).clamp(min=1e-4).view(box_count, 2, 1, 1)
        else:
            weight = (
                (boxes[:, 2:] ** 2)
                .min(dim=1, keepdim=True)[0]
                .clamp(min=1e-4)
                .view(box_count, 1, 1, 1)
            )

        mounts = torch.exp(
            -(((grid_xy - grid_cxcy.long()) ** 2) / weight).sum(dim=1, keepdim=True)
            / (2 * self.sigma ** 2)
        )
        heatmap, _ = mounts.max(dim=0, keepdim=True)
        return Heatmaps(heatmap)

    @torch.no_grad()
    def __call__(
        self,
        box_batch: List[YoloBoxes],
        hw: Tuple[int, int],
        original_hw: Tuple[int, int],
    ) -> Heatmaps:
        hms: List[Tensor] = []
        for boxes in box_batch:
            hm = self._mkmaps(boxes, hw, original_hw)
            hms.append(hm)

        return Heatmaps(torch.cat(hms, dim=0))


class Criterion:
    def __init__(
        self,
        box_weight: float = 4.0,
        heatmap_weight: float = 1.0,
        sigma: float = 1.0,
        iou_threshold: float = 0.1,
    ) -> None:
        self.box_weight = box_weight
        self.heatmap_weight = heatmap_weight

        self.hm_loss = HMLoss()
        self.box_loss = BoxLoss(iou_threshold=iou_threshold)
        self.mkmaps = MkMaps(sigma)

    def __call__(
        self, images: ImageBatch, net_output: NetOutput, gt_boxes_list: List[YoloBoxes],
    ) -> Tuple[Tensor, Tensor, Tensor, Heatmaps]:
        anchors, box_diffs, s_hms = net_output
        device = box_diffs.device
        box_losses: List[Tensor] = []
        hm_losses: List[Tensor] = []
        _, _, orig_h, orig_w = images.shape
        _, _, h, w = s_hms.shape

        t_hms = self.mkmaps(gt_boxes_list, (h, w), (orig_h, orig_w))
        hm_loss = self.hm_loss(s_hms, t_hms) * self.heatmap_weight

        for box_diff, heatmap, gt_boxes in zip(box_diffs, s_hms, gt_boxes_list):
            if len(gt_boxes) == 0:
                continue
            box_losses.append(
                self.box_loss(
                    anchors=anchors,
                    gt_boxes=gt_boxes,
                    box_diff=BoxDiff(box_diff),
                    heatmap=Heatmap(heatmap),
                )
            )
        box_loss = torch.stack(box_losses).mean() * self.box_weight
        loss = hm_loss + box_loss
        return loss, hm_loss, box_loss, Heatmaps(t_hms)


class Trainer:
    def __init__(
        self,
        model: nn.Module,
        train_loader: DataLoader,
        test_loader: DataLoader,
        model_loader: ModelLoader,
        best_watcher: BestWatcher,
        visualize: Visualize,
        optimizer: Any,
        get_score: Callable[[YoloBoxes, YoloBoxes], float],
        to_boxes: ToBoxes,
        device: str = "cpu",
        criterion: Criterion = Criterion(),
    ) -> None:
        self.device = torch.device(device)
        self.model_loader = model_loader
        self.model = model.to(self.device)
        self.preprocess = PreProcess(self.device)
        self.to_boxes = to_boxes
        self.optimizer = optimizer
        self.train_loader = train_loader
        self.test_loader = test_loader
        self.criterion = criterion
        self.best_watcher = best_watcher
        self.visualize = visualize
        self.get_score = get_score
        self.meters = {
            key: MeanMeter()
            for key in [
                "train_loss",
                "train_box",
                "train_hm",
                "test_loss",
                "test_box",
                "test_hm",
                "score",
            ]
        }

        if model_loader.check_point_exists():
            self.model, meta = model_loader.load(self.model)
            self.best_watcher.step(meta["score"])

    def log(self) -> None:
        value = ("|").join([f"{k}:{v.get_value():.4f}" for k, v in self.meters.items()])
        logger.info(value)

    def reset_meters(self) -> None:
        for v in self.meters.values():
            v.reset()

    def train(self, num_epochs: int) -> None:
        for epoch in range(num_epochs):
            self.train_one_epoch()
            self.eval_one_epoch()
            self.log()
            self.reset_meters()

    def train_one_epoch(self) -> None:
        self.model.train()
        loader = self.train_loader
        for samples, gt_boxes_list, ids, _ in tqdm(loader):
            samples, gt_boxes_list = self.preprocess((samples, gt_boxes_list))
            outputs = self.model(samples)
            loss, box_loss, hm_loss, _ = self.criterion(samples, outputs, gt_boxes_list)
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

            self.meters["train_loss"].update(loss.item())
            self.meters["train_box"].update(box_loss.item())
            self.meters["train_hm"].update(hm_loss.item())
        preds = self.to_boxes(outputs)

    @torch.no_grad()
    def eval_one_epoch(self) -> None:
        self.model.train()
        loader = self.test_loader
        for images, box_batch, ids, _ in tqdm(loader):
            images, box_batch = self.preprocess((images, box_batch))
            outputs = self.model(images)
            loss, box_loss, hm_loss, gt_hms = self.criterion(images, outputs, box_batch)
            self.meters["test_loss"].update(loss.item())
            self.meters["test_box"].update(box_loss.item())
            self.meters["test_hm"].update(hm_loss.item())
            preds = self.to_boxes(outputs)
            for (pred, gt) in zip(preds[0], box_batch):
                self.meters["score"].update(self.get_score(pred, gt))

        self.visualize(outputs, preds, box_batch, images, gt_hms)
        if self.best_watcher.step(self.meters["score"].get_value()):
            self.model_loader.save(
                self.model, {"score": self.meters["score"].get_value()}
            )

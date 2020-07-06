import torch
from torch.utils.data import DataLoader
from object_detection.entities import PyramidIdx
from object_detection.models.centernet import (
    collate_fn,
    CenterNet,
    Visualize,
    Trainer,
    Criterion,
    ToBoxes,
)
from object_detection.models.backbones.resnet import ResNetBackbone
from object_detection.model_loader import ModelLoader
from object_detection.data.object import ObjectDataset
from object_detection.metrics import MeanPrecition
from object_detection.meters import BestWatcher
from logging import getLogger, StreamHandler, Formatter, INFO, FileHandler

logger = getLogger()
logger.setLevel(INFO)
stream_handler = StreamHandler()
stream_handler.setLevel(INFO)
handler_format = Formatter("%(asctime)s|%(name)s|%(message)s")
stream_handler.setFormatter(handler_format)
logger.addHandler(stream_handler)

### config ###
sigma = 4.0
batch_size = 8
out_idx: PyramidIdx = 3
threshold = 0.1
channels = 256
input_size = 256
object_count_range = (1, 20)
object_size_range = (32, 64)
### config ###

train_dataset = ObjectDataset(
    (input_size, input_size),
    object_count_range=object_count_range,
    object_size_range=object_size_range,
    num_samples=1024,
)
test_dataset = ObjectDataset(
    (input_size, input_size),
    object_count_range=object_count_range,
    object_size_range=object_size_range,
    num_samples=256,
)
backbone = ResNetBackbone("resnet50", out_channels=channels)
model = CenterNet(channels=channels, backbone=backbone, out_idx=out_idx, depth=1)
model_loader = ModelLoader("/store/centernet")
criterion = Criterion(sizemap_weight=1.0, sigma=sigma)
optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4)
visualize = Visualize("/store/centernet", "test", limit=2)
best_watcher = BestWatcher(mode="max")
to_boxes = ToBoxes(threshold=threshold, limit=60)
get_score = MeanPrecition()
trainer = Trainer(
    model=model,
    train_loader=DataLoader(
        train_dataset, collate_fn=collate_fn, batch_size=batch_size, shuffle=True
    ),
    test_loader=DataLoader(
        test_dataset, collate_fn=collate_fn, batch_size=batch_size, shuffle=True
    ),
    model_loader=model_loader,
    optimizer=optimizer,
    visualize=visualize,
    criterion=criterion,
    best_watcher=best_watcher,
    device="cuda",
    get_score=get_score,
    to_boxes=to_boxes,
)
trainer.train(500)

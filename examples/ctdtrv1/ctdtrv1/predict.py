from object_detection.models.centernetv1 import Predicter, prediction_collate_fn
from object_detection.data.object import PredictionDataset
from torch.utils.data import DataLoader
from . import config as cfg
from . import train

dataset = PredictionDataset(
    cfg.input_size,
    object_count_range=cfg.object_count_range,
    object_size_range=cfg.object_size_range,
    num_samples=1024,
)

data_loader = DataLoader(
    dataset=dataset,
    collate_fn=prediction_collate_fn,
    batch_size=cfg.batch_size,
    shuffle=True,
)

predictor = Predicter(
    model=train.model,
    loader=data_loader,
    model_loader=train.model_loader,
    device="cuda",
    box_merge=train.box_merge,
    to_boxes=train.to_boxes,
)

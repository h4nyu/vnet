import torch
from app.dataset.wheat import WheatDataset
from app.models.centernet import PreProcess
from torch.utils.data import DataLoader
from app import config
from pathlib import Path
from app.utils import DetectionPlot


def test_plotrow() -> None:
    ...
    #  images = load_lables()
    #  dataset = WheatDataset(images,)
    #  for i in range(5):
    #      img, annots = dataset[1]
    #      plot = DetectionPlot(figsize=(6, 6))
    #      plot.with_image(img)
    #      plot.with_boxes(annots["boxes"], color="red")
    #      plot.save(str(Path(config.plot_dir).joinpath(f"test-{i}.png")))


def test_prediction_dataset() -> None:
    ...
    #  ds = PreditionDataset()
    #  row = ds[0]
    #  print(row)

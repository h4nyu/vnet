from typing import Tuple
from object_detection.entities import PyramidIdx
from object_detection.model_loader import WatchMode

confidence_threshold = 0.5
batch_size = 16
channels = 128

input_size = (256, 256)
object_count_range = (1, 20)
object_size_range = (32, 64)
iou_threshold = 0.55
out_dir = "/store/efficientdet"
metric: Tuple[str, WatchMode] = ("score", "max")

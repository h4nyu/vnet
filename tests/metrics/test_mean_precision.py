import torch
import numpy as np
from object_detection import (
    CoCoBoxes,
    YoloBoxes,
    coco_to_pascal,
    Confidences,
)
from object_detection.metrics import MeanPrecition

gts = np.array(
    [
        [954, 391, 70, 90],
        [660, 220, 95, 102],
        [64, 209, 76, 57],
        [896, 99, 102, 69],
        [747, 460, 72, 77],
        [885, 163, 103, 69],
        [514, 399, 90, 97],
        [702, 794, 97, 99],
        [721, 624, 98, 108],
        [826, 512, 82, 94],
        [883, 944, 79, 74],
        [247, 594, 123, 92],
        [673, 514, 95, 113],
        [829, 847, 102, 110],
        [94, 737, 92, 107],
        [588, 568, 75, 107],
        [158, 890, 103, 64],
        [744, 906, 75, 79],
        [826, 33, 72, 74],
        [601, 69, 67, 87],
    ]
)
preds = np.array(
    [
        [956, 409, 68, 85],
        [883, 945, 85, 77],
        [745, 468, 81, 87],
        [658, 239, 103, 105],
        [518, 419, 91, 100],
        [711, 805, 92, 106],
        [62, 213, 72, 64],
        [884, 175, 109, 68],
        [721, 626, 96, 104],
        [878, 619, 121, 81],
        [887, 107, 111, 71],
        [827, 525, 88, 83],
        [816, 868, 102, 86],
        [166, 882, 78, 75],
        [603, 563, 78, 97],
        [744, 916, 68, 52],
        [582, 86, 86, 72],
        [79, 715, 91, 101],
        [246, 586, 95, 80],
        [181, 512, 93, 89],
        [655, 527, 99, 90],
        [568, 363, 61, 76],
        [9, 717, 152, 110],
        [576, 698, 75, 78],
        [805, 974, 75, 50],
        [10, 15, 78, 64],
        [826, 40, 69, 74],
        [32, 983, 106, 40],
    ]
)
scores = np.array(
    [
        0.9932319,
        0.99206185,
        0.99145633,
        0.9898089,
        0.98906296,
        0.9817738,
        0.9799762,
        0.97967803,
        0.9771589,
        0.97688967,
        0.9562935,
        0.9423076,
        0.93556845,
        0.9236257,
        0.9102379,
        0.88644403,
        0.8808225,
        0.85238415,
        0.8472188,
        0.8417798,
        0.79908705,
        0.7963756,
        0.7437897,
        0.6044758,
        0.59249884,
        0.5557045,
        0.53130984,
        0.5020239,
    ]
)


def test_mean_precision() -> None:
    pred_boxes = coco_to_pascal(CoCoBoxes(torch.from_numpy(preds)))
    gt_boxes = coco_to_pascal(CoCoBoxes(torch.from_numpy(gts)))
    fn = MeanPrecition()
    res = fn(pred_boxes, gt_boxes)
    assert res < 0.37

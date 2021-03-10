from setuptools import setup

setup(
    name="vnet",
    version="0.1.5",
    install_requires=[
        "tqdm",
        "opencv-python",
        "efficientnet_pytorch",
        "torchvision",
        "torch",
    ],
    extras_require={
        "develop": [
            "pytest",
            "black",
            "pytest-cov",
            "pytest-benchmark",
            "mypy",
            "kaggle",
            "pandas",
            "cytoolz",
            "torch-optimizer",
            "albumentations",
            "scikit-learn",
        ]
    },
)

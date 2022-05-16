from fire import Fire

from inputs.datasets.q_dataset import Dataset
from models.joint_decoding.q_decoder import EntRelJointDecoder
from q_main import evaluate


def binary_search(fn, left: float, right: float, threshold: float):
    mid = (left + right) / 2
    if abs(fn(left) - fn(mid)) < threshold:
        return mid
    if fn(left) > fn(right):
        return binary_search(fn, left, mid, threshold)
    else:
        return binary_search(fn, mid, right, threshold)


def run_eval(
    path: str = "ckpt/quintuplet/best_model",
    path_data="ckpt/quintuplet/dataset.pickle",
    data_split: str = "dev",
):
    model = EntRelJointDecoder.load(path)
    dataset = Dataset.load(path_data)
    cfg = model.cfg
    evaluate(cfg, dataset, model, data_split)


"""
p q_predict.py run_eval --data_split dev
p q_predict.py run_eval --data_split test

p analysis.py test_preds \
--path_pred ckpt/quintuplet/raw_dev.pkl \
--path_gold data/quintuplet/dev.json \
--path_vocab ckpt/quintuplet/vocabulary.pickle

{
  "num_correct": 3566,
  "num_pred": 5167,
  "num_gold": 6203,
  "precision": 0.6901490226437004,
  "recall": 0.5748831210704498,
  "f1": 0.6272647317502199
}

p analysis.py test_preds \
--path_pred ckpt/quintuplet/raw_test.pkl \
--path_gold data/quintuplet/test.json \
--path_vocab ckpt/quintuplet/vocabulary.pickle

{
  "num_correct": 3639,
  "num_pred": 5199,
  "num_gold": 6093,
  "precision": 0.6999422965954991,
  "recall": 0.5972427375677006,
  "f1": 0.6445270988310309
}

"""


if __name__ == "__main__":
    Fire()

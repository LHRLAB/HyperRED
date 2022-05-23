import json
import random
from collections import Counter
from pprint import pprint
from typing import List

import numpy as np
import torch
from fire import Fire
from tqdm import tqdm
from transformers.models.auto.modeling_auto import AutoModel
from transformers.models.auto.tokenization_auto import AutoTokenizer

from data.q_process import Sentence, SparseCube, load_raw_preds
from inputs.datasets.q_dataset import Dataset
from models.joint_decoding.q_decoder import decode_nonzero_cuboids
from models.joint_decoding.q_tagger import decode_nonzero_spans


def test_lengths(
    path: str = "json/dev.json",
    name: str = "bert-base-uncased",
    percentile: float = 0.95,
):
    texts = []

    with open(path) as f:
        for line in f:
            raw = json.loads(line)
            for lst in raw["sentences"]:
                texts.append(" ".join(lst))

    random.seed(0)
    for t in random.sample(texts, k=10):
        print(t)

    tokenizer = AutoTokenizer.from_pretrained(name)
    lengths = [len(tokenizer(t).input_ids) for t in tqdm(texts)]
    value = sorted(lengths)[round(len(texts) * percentile)]
    print(dict(value=value, percentile=percentile, min=min(lengths), max=max(lengths)))


def test_table_size(
    batch_size: int = 32,
    seq_len: int = 48,
    hidden_size: int = 151,
    num_labels: int = 14,
):
    head = torch.rand(batch_size, seq_len, hidden_size).cuda()
    tail = torch.rand(batch_size, seq_len, hidden_size).cuda()
    U = torch.rand(num_labels, hidden_size, hidden_size).cuda()

    triplet_score = torch.einsum("bxi, oij, byj -> bxyo", head, U, tail)
    print(dict(triplet_score=triplet_score.shape))  # (batch, len, len, labels)

    value = torch.rand(batch_size, seq_len, hidden_size).cuda()
    V = torch.zeros(num_labels, hidden_size * 2, hidden_size).cuda()
    pair = torch.cat(
        [
            head.unsqueeze(dim=2).expand(-1, -1, seq_len, -1),
            tail.unsqueeze(dim=1).expand(-1, seq_len, -1, -1),
        ],
        dim=-1,
    )
    final_score = torch.einsum("bxyi, oij, bzj -> bxyzo", pair, V, value)
    print(dict(final_score=final_score.shape))


def test_nyt(
    path="nyt/train.json",
    path_words="nyt/words2id.json",
    path_relations="nyt/relations2id.json",
):
    with open(path_words) as f:
        w2i = json.load(f)
        i2w = {i: w for w, i in w2i.items()}

    with open(path_relations) as f:
        r2i = json.load(f)
        i2r = {i: r for r, i in r2i.items()}

    with open(path) as f:
        raw = json.load(f)
        a, b, c = raw
        assert len(a) == len(b)
        assert len(a) == len(c)

        for i in tqdm(range(len(a))):
            words = [i2w[_] for _ in b[i]]
            assert len(c[i]) % 3 == 0
            assert len(c[i]) > 0

            for j in range(0, len(c[i]), 3):
                head, tail, k = c[i][j : j + 3]
                relation = i2r[k]
                info = dict(
                    a=a[i],
                    a_word=i2w[a[i]],
                    text=" ".join(words),
                    head=words[head],
                    tail=words[tail],
                    relation=relation,
                )
                print(json.dumps(info, indent=2))


def test_data(path: str = "data/ACE2005/test.json"):
    with open(path) as f:
        sents = [Sentence(**json.loads(line)) for line in f]
    print(dict(sents=len(sents)))

    for s in sents:
        if s.relationMentions:
            for k, v in s.dict().items():
                print(k, str(v)[:120])
            break

    print("\nHow many have span overlap?")
    print(len([s for s in sents if s.check_span_overlap()]))


def test_quintuplet_sents(path: str = "data/quintuplet/dev.json"):
    with open(path) as f:
        sents = [Sentence(**json.loads(line)) for line in tqdm(f)]

    print("\nHow many entities per sentence?")
    lengths = [len(s.entityMentions) for s in sents]
    print(np.mean(lengths))

    print("\nWhat fraction of the cubes (quintuplets) are empty?")
    total = 0
    filled = 0
    sizes = []
    counts = []
    for s in sents:
        assert s.quintupletMatrix is not None
        total += s.quintupletMatrix.numel()
        sizes.append(s.quintupletMatrix.numel())
        filled += len(s.quintupletMatrix.entries)
        counts.append(len(s.quintupletMatrix.entries))
    print(dict(frac=1 - (filled / total), sizes=np.mean(sizes), counts=np.mean(counts)))

    print("\nWhat fraction of the tables (relations) are empty?")
    total = 0
    filled = 0
    for s in sents:
        table = np.array(s.jointLabelMatrix)
        total += table.size
        filled += np.sum(table > 0)
    print(1 - (filled / total))

    print("\nWhat fraction of the diagonals (entities) are empty?")
    total = 0
    filled = 0
    for s in sents:
        row = list(np.diagonal(np.array(s.jointLabelMatrix)))
        total += len(row)
        filled += sum(row)
    print(1 - (filled / total))

    print("\nWhat is the average sentence length?")
    lengths = [len(s.sentText.split()) for s in sents]
    print(sum(lengths) / len(lengths))

    print("\nWhat is the average cube length?")
    lengths = [s.quintupletMatrix.shape[0] for s in sents]
    print(sum(lengths) / len(lengths))

    print("\nWhat is the average number of entity tokens in a sentence?")
    lengths = []
    for s in sents:
        tags = [0 for _ in s.sentText.split()]
        for e in s.entityMentions:
            for i in range(e.offset[0], e.offset[1]):
                tags[i] = 1
        lengths.append(sum(tags))
        assert list(np.diagonal(np.array(s.jointLabelMatrix))) == tags
    print(sum(lengths) / len(lengths))

    print("\nWhat is average entity length?")
    lengths = []
    for s in sents:
        for e in s.entityMentions:
            start, end = e.offset
            assert end > start
            lengths.append(end - start)
    print(dict(lengths=np.mean(lengths)))

    print("\nHow many quintuplets per sent on average?")
    lengths = [len(s.qualifierMentions) for s in sents]
    print(dict(lengths=np.mean(lengths)))

    print("\nManually analyze cube")
    sizes = []
    counts = []
    for s in sents:
        seq_len = len(s.sentText.split())
        cube = np.zeros(shape=(seq_len, seq_len, seq_len))
        id_to_span = {e.emId: e.offset for e in s.entityMentions}
        for q in s.qualifierMentions:
            head = id_to_span[q.em1Id]
            tail = id_to_span[q.em2Id]
            value = id_to_span[q.em3Id]
            assert len(set([head, tail, value])) == 3
            for i in range(*head):
                for j in range(*tail):
                    for k in range(*value):
                        cube[i, j, k] = 1
        sizes.append(cube.size)
        counts.append(cube.sum())
    print(
        dict(
            frac=sum(counts) / sum(sizes), sizes=np.mean(sizes), counts=np.mean(counts)
        )
    )

    print("\nWhat fraction of sentences have overlapping entities?")
    selected = []
    seen = set()
    for s in sents:
        tags = [0 for _ in s.sentText.split()]
        for e in s.entityMentions:
            for i in range(e.offset[0], e.offset[1]):
                if tags[i] == 1 and s.sentText not in seen:
                    seen.add(s.sentText)
                    selected.append(s)
                else:
                    tags[i] = 1
    print(dict(frac=len(selected) / len(sents)))

    print("\nIf restrict to top-50 qualifiers, how many quintuplets are affected?")
    top_k = 50
    qualifiers = []
    for s in sents:
        for q in s.qualifierMentions:
            qualifiers.append(q.label)
    counter = Counter(qualifiers)
    threshold = sorted(counter.values())[-top_k]
    remainder = sum(v for v in counter.values() if v >= threshold)
    print(dict(threshold=threshold, remainder=remainder, total=len(qualifiers)))


def test_sparse_cube(path: str = "data/q10/dev.json"):
    with open(path) as f:
        for line in tqdm(f.readlines()):
            sent = Sentence(**json.loads(line))
            matrix = sent.quintupletMatrix
            x = matrix.numpy()
            new = SparseCube.from_numpy(x)
            if not matrix.check_equal(new):
                print("Rarely (0.001), orig cube has multiple entries in same i,j,k")


def test_raw_q_preds(path: str = "ckpt/q10/raw_test.pkl"):
    preds = load_raw_preds(path)
    print("\nHow many preds have at least one q_matrix entry?")
    num = sum(1 for p in preds if len(p.quintuplet_preds.entries) > 0)
    print(dict(total=len(preds), num=num))


def test_decode_nonzero_spans():
    for labels in [[0, 0, 0], [0, 0, 1, 2], [0, 1, 1, 0], [1, 0, 0, 1], [1, 1, 1]]:
        spans = decode_nonzero_spans(labels)
        print(dict(labels=labels, spans=spans, values=[labels[a:b] for a, b in spans]))


def analyze_sents(sents: List[Sentence]) -> dict:
    relations = [r.label for s in sents for r in s.relationMentions]
    qualifiers = [q.label for s in sents for q in s.qualifierMentions]
    entity_labels = [e.label for s in sents for e in s.entityMentions]
    info = dict(
        triplets=len(relations),
        quintuplets=len(qualifiers),
        ents=len(entity_labels),
        relations=len(set(relations)),
        qualifiers=len(set(qualifiers)),
        entity_labels=len(set(entity_labels)),
    )
    return info


def compare_tag_data(
    path_tag: str = "data/q10_tagger/dev.json", path_orig="data/q10/dev.json"
):
    with open(path_orig) as f:
        sents_orig = [Sentence(**json.loads(line)) for line in f]
    with open(path_tag) as f:
        sents_tag = [Sentence(**json.loads(line)) for line in f]

    print("\nOrig stats?")
    print(json.dumps(analyze_sents(sents_orig)))
    print("\nNew stats?")
    print(json.dumps(analyze_sents(sents_tag)))

    print("\nCan the spans in table be decoded correctly?")
    decoded_spans = []
    correct_spans = []
    for s in sents_tag:
        labels = np.array(s.jointLabelMatrix).diagonal()
        gold = set(e.offset for e in s.entityMentions)
        spans = decode_nonzero_spans([int(x) for x in labels])
        decoded_spans.extend(spans)
        correct_spans.extend([sp for sp in spans if sp in gold])
    print(dict(decoded_spans=len(decoded_spans), correct_spans=len(correct_spans)))


def test_decode_nonzero_cuboids(path: str = "data/q10/dev.json"):
    with open(path) as f:
        sents = [Sentence(**json.loads(line)) for line in tqdm(f.readlines())]

    cubes = [torch.tensor(s.quintupletMatrix.numpy()) for s in tqdm(sents)]
    for i, c in enumerate(tqdm(cubes, desc="nonzero")):
        assert c.nonzero().shape[0] > 0
        cuboids = decode_nonzero_cuboids(c)
        if len(sents[i].qualifierMentions) != len(cuboids):
            pprint(sents[i].qualifierMentions)
            pprint(cuboids)
            print()


def test_roberta(path: str = "ckpt/q30r/dataset.pickle"):
    device = torch.device("cpu")
    # device = torch.device("cuda")
    ds = Dataset.load(path)
    bs = 2
    model = AutoModel.from_pretrained("roberta-base")
    model = model.to(device)

    for epoch, batch in ds.get_batch("train", bs, None):
        a = torch.tensor(batch["wordpiece_tokens"], device=device)
        b = torch.tensor(batch["wordpiece_segment_ids"], device=device)
        mask = (a != 0).long()
        outputs = model(input_ids=a, attention_mask=mask)
        # outputs = model(input_ids=a, token_type_ids=b, attention_mask=mask)
        print(dict(epoch=epoch, **{k: v.shape for k, v in outputs.items()}))
        breakpoint()


"""
Roberta experiments

################################################################################
Roberta q10 and q30
 
rm -rf temp
p data/q_process.py make_sentences ../quintuplet/outputs/data/flat_min_10/train.json temp/train.json
p data/q_process.py make_sentences ../quintuplet/outputs/data/flat_min_10/dev.json temp/dev.json
p data/q_process.py make_sentences ../quintuplet/outputs/data/flat_min_10/test.json temp/test.json
p data/q_process.py make_label_file "temp/*.json" data/q10r/label.json

p data/q_process.py process temp/train.json data/q10r/train.json data/q10r/label.json roberta-base
p data/q_process.py process temp/dev.json data/q10r/dev.json data/q10r/label.json roberta-base
p data/q_process.py process temp/test.json data/q10r/test.json data/q10r/label.json roberta-base

rm -rf temp
p data/q_process.py make_sentences ../quintuplet/outputs/data/flat_min_30/train.json temp/train.json
p data/q_process.py make_sentences ../quintuplet/outputs/data/flat_min_30/dev.json temp/dev.json
p data/q_process.py make_sentences ../quintuplet/outputs/data/flat_min_30/test.json temp/test.json
p data/q_process.py make_label_file "temp/*.json" data/q30r/label.json

p data/q_process.py process temp/train.json data/q30r/train.json data/q30r/label.json roberta-base
p data/q_process.py process temp/dev.json data/q30r/dev.json data/q30r/label.json roberta-base
p data/q_process.py process temp/test.json data/q30r/test.json data/q30r/label.json roberta-base

################################################################################

p q_main.py \
--embedding_model pretrained \
--pretrained_model_name roberta-base \
--ent_rel_file label.json \
--train_batch_size 16 \
--gradient_accumulation_steps 2 \
--config_file config.yml \
--save_dir ckpt/q10r \
--data_dir data/q10r \
--fine_tune \
--max_sent_len 80 \
--max_wordpiece_len 80 \
--epochs 30 \
--pretrain_epochs 0 \
--device 0

p q_predict.py run_eval ckpt/q10r/best_model ckpt/q10r/dataset.pickle test

{
  "entity": { 
    "num_correct": 4798,
    "num_pred": 5303,  
    "num_gold": 6432,
    "precision": 0.9047708844050537,
    "recall": 0.7459577114427861,
    "f1": 0.8177247550063912
  },
  "strict triplet": {
    "num_correct": 1477,
    "num_pred": 1897,
    "num_gold": 2312,
    "precision": 0.7785977859778598,
    "recall": 0.638840830449827,
    "f1": 0.7018294131622713
  },
  "quintuplet": {
    "num_correct": 1316,
    "num_pred": 2120,
    "num_gold": 2595,
    "precision": 0.620754716981132,
    "recall": 0.5071290944123314,
    "f1": 0.558218451749735
  }
}

p q_main.py \
--embedding_model pretrained \
--pretrained_model_name roberta-base \
--ent_rel_file label.json \
--train_batch_size 16 \
--gradient_accumulation_steps 2 \
--config_file config.yml \
--save_dir ckpt/q30r \
--data_dir data/q30r \
--fine_tune \
--max_sent_len 80 \
--max_wordpiece_len 80 \
--epochs 30 \
--pretrain_epochs 0 \
--device 0

p q_predict.py run_eval ckpt/q30r/best_model ckpt/q30r/dataset.pickle test

{                                                           
  "entity": {
    "num_correct": 4469,
    "num_pred": 4925,
    "num_gold": 5777,
    "precision": 0.9074111675126904,                        
    "recall": 0.7735849056603774,                           
    "f1": 0.8351709960754999
  },                   
  "strict triplet": { 
    "num_correct": 1354,
    "num_pred": 1715,                                       
    "num_gold": 2060,       
    "precision": 0.7895043731778426,                        
    "recall": 0.6572815533980583,
    "f1": 0.7173509933774834                                
  },                                                        
  "quintuplet": {
    "num_correct": 1235,   
    "num_pred": 1956,
    "num_gold": 2302,
    "precision": 0.6313905930470347,
    "recall": 0.5364900086880973,
    "f1": 0.5800845467355565
  }
}

Findings
- FP16 doesn't significantly change speed or results

Tasks
- debug roberta
- use distil-bert/roberta for pipeline?
- position embeddings
- pruning / cuboid dropout?

"""


if __name__ == "__main__":
    Fire()

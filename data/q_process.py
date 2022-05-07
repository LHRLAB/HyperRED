import json
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

import fire
import numpy as np
from pydantic import BaseModel
from tqdm import tqdm
from transformers.models.auto.tokenization_auto import AutoTokenizer

Span = Tuple[int, int]


class FlatQuintuplet(BaseModel):
    tokens: List[str]
    head: Span
    tail: Span
    value: Span
    relation: str
    qualifier: str

    @property
    def text(self) -> str:
        return " ".join(self.tokens)


def load_quintuplets(path: str) -> List[FlatQuintuplet]:
    with open(path) as f:
        return [FlatQuintuplet(**json.loads(line)) for line in f]


def make_label_file(
    data_pattern: str = "../quintuplet/outputs/data/flat/*.json",
    path_out: str = "data/quintuplet/ent_rel_file.json",
):
    paths = sorted(Path().glob(data_pattern))
    quintuplets = [q for p in paths for q in load_quintuplets(str(p))]
    relations = [q.relation for q in quintuplets]
    qualifiers = [q.qualifier for q in quintuplets]
    labels = ["None", "Entity"]
    labels.extend(sorted(set(relations + qualifiers)))
    label_map = {name: i for i, name in enumerate(labels)}

    entity_ids = [label_map["Entity"]]
    relation_ids = sorted(set(label_map[name] for name in relations))
    qualifier_ids = sorted(set(label_map[name] for name in qualifiers))

    num_none = 0
    num_entity = len(quintuplets) * 3  # Three entities per quintuplet
    counter = Counter(relations + qualifiers)
    counts = [num_none, num_entity] + [
        counter[name] for name in sorted(set(relations + qualifiers))
    ]

    assert len(counts) == len(label_map.keys())
    info = dict(
        id=label_map,
        entity=entity_ids,
        relation=relation_ids,
        qualifier=qualifier_ids,
        symmetric=[],
        asymmetric=sorted(set(relation_ids + qualifier_ids)),
        count=counts,
    )
    Path(path_out).parent.mkdir(exist_ok=True, parents=True)
    with open(path_out, "w") as f:
        f.write(json.dumps(info, indent=2))


class Entity(BaseModel):
    emId: str
    text: str
    offset: Span  # Token spans, start inclusive, end exclusive
    label: str


class Relation(BaseModel):
    em1Id: str
    em1Text: str
    em2Id: str
    em2Text: str
    label: str


class Qualifier(BaseModel):
    em1Id: str
    em2Id: str
    em3Id: str
    label: str


class SparseCube(BaseModel):
    shape: Tuple[int, int, int]
    entries: List[Tuple[int, int, int, int]]

    def numpy(self) -> np.ndarray:
        x = np.zeros(shape=self.shape)
        for i, j, k, value in self.entries:
            x[i, j, k] = value
        return x

    def tolist(self) -> List[List[List[int]]]:
        x = self.numpy()
        return [[list(row) for row in table] for table in x]

    def numel(self) -> int:
        i, j, k = self.shape
        return i * j * k

    @classmethod
    def empty(cls):
        return cls(shape=(0, 0, 0), entries=[])


class Sentence(BaseModel):
    articleId: str
    sentId: int
    sentText: str
    entityMentions: List[Entity]
    relationMentions: List[Relation]
    qualifierMentions: List[Qualifier]
    wordpieceSentText: str
    wordpieceTokensIndex: List[Span]
    wordpieceSegmentIds: List[int]
    jointLabelMatrix: List[List[int]]
    quintupletMatrix: SparseCube


def make_sentences(path_in: str, path_out: str):
    quintuplets = load_quintuplets(path_in)
    groups: Dict[str, List[FlatQuintuplet]] = {}
    for q in quintuplets:
        groups.setdefault(q.text, []).append(q)

    sentences: List[Sentence] = []
    for lst in tqdm(list(groups.values())):
        span_to_entity: Dict[Span, Entity] = {}
        pair_to_relation: Dict[Tuple[Span, Span], Relation] = {}
        triplet_to_qualifier: Dict[Tuple[Span, Span, Span], Qualifier] = {}

        for q in lst:
            for span in [q.head, q.tail, q.value]:
                ent = Entity(
                    offset=span,
                    emId=str(span),
                    text=" ".join(q.tokens[span[0] : span[1]]),
                    label="Entity",
                )
                span_to_entity[span] = ent

        for q in lst:
            head = span_to_entity[q.head]
            tail = span_to_entity[q.tail]
            value = span_to_entity[q.value]
            relation = Relation(
                em1Id=head.emId,
                em1Text=head.text,
                em2Id=tail.emId,
                em2Text=tail.text,
                label=q.relation,
            )
            qualifier = Qualifier(
                em1Id=head.emId, em2Id=tail.emId, em3Id=value.emId, label=q.qualifier
            )
            pair_to_relation[(head.offset, tail.offset)] = relation
            triplet_to_qualifier[(head.offset, tail.offset, value.offset)] = qualifier

        sent = Sentence(
            articleId=lst[0].text,
            sentId=0,
            sentText=lst[0].text,
            entityMentions=list(span_to_entity.values()),
            relationMentions=list(pair_to_relation.values()),
            qualifierMentions=list(triplet_to_qualifier.values()),
            wordpieceSentText="",
            wordpieceTokensIndex=[],
            wordpieceSegmentIds=[],
            jointLabelMatrix=[],
            quintupletMatrix=SparseCube.empty(),
        )
        sentences.append(sent)

    Path(path_out).parent.mkdir(exist_ok=True, parents=True)
    with open(path_out, "w") as f:
        for sent in tqdm(sentences):
            f.write(sent.json() + "\n")


def add_tokens(sent, tokenizer):
    cls = tokenizer.cls_token
    sep = tokenizer.sep_token
    wordpiece_tokens = [cls]
    wordpiece_tokens.append(sep)

    context_len = len(wordpiece_tokens)
    wordpiece_segment_ids = [0] * context_len

    wordpiece_tokens_index = []
    cur_index = len(wordpiece_tokens)
    for token in sent["sentText"].split(" "):
        tokenized_token = list(tokenizer.tokenize(token))
        wordpiece_tokens.extend(tokenized_token)
        wordpiece_tokens_index.append([cur_index, cur_index + len(tokenized_token)])
        cur_index += len(tokenized_token)
    wordpiece_tokens.append(sep)
    wordpiece_segment_ids += [1] * (len(wordpiece_tokens) - context_len)

    sent.update(
        {
            "wordpieceSentText": " ".join(wordpiece_tokens),
            "wordpieceTokensIndex": wordpiece_tokens_index,
            "wordpieceSegmentIds": wordpiece_segment_ids,
        }
    )
    return sent


def add_joint_label(sent, ent_rel_id):
    """add_joint_label add joint labels for sentences"""

    none_id = ent_rel_id["None"]
    seq_len = len(sent["sentText"].split(" "))
    label_matrix = [[none_id for j in range(seq_len)] for i in range(seq_len)]

    ent2offset = {}
    for ent in sent["entityMentions"]:
        ent2offset[ent["emId"]] = ent["offset"]
        for i in range(ent["offset"][0], ent["offset"][1]):
            for j in range(ent["offset"][0], ent["offset"][1]):
                label_matrix[i][j] = ent_rel_id[ent["label"]]
    for rel in sent["relationMentions"]:
        for i in range(ent2offset[rel["em1Id"]][0], ent2offset[rel["em1Id"]][1]):
            for j in range(ent2offset[rel["em2Id"]][0], ent2offset[rel["em2Id"]][1]):
                label_matrix[i][j] = ent_rel_id[rel["label"]]

    entries: List[Tuple[int, int, int, int]] = []
    for rel in sent["qualifierMentions"]:
        for i in range(ent2offset[rel["em1Id"]][0], ent2offset[rel["em1Id"]][1]):
            for j in range(ent2offset[rel["em2Id"]][0], ent2offset[rel["em2Id"]][1]):
                for k in range(
                    ent2offset[rel["em3Id"]][0], ent2offset[rel["em3Id"]][1]
                ):
                    entries.append((i, j, k, ent_rel_id[rel["label"]]))

    sent["jointLabelMatrix"] = label_matrix
    sent["quintupletMatrix"] = SparseCube(
        shape=(seq_len, seq_len, seq_len), entries=entries
    ).dict()
    return sent


def process(
    source_file,
    target_file,
    ent_rel_file: str = "data/quintuplet/ent_rel_file.json",
    pretrained_model: str = "bert-base-uncased",
):
    auto_tokenizer = AutoTokenizer.from_pretrained(pretrained_model)
    print("Load {} tokenizer successfully.".format(pretrained_model))

    with open(ent_rel_file) as f:
        ids = json.load(f)["id"]

    with open(source_file) as fin, open(target_file, "w") as fout:
        for line in tqdm(fin.readlines()):
            sent = json.loads(line.strip())
            sent = add_tokens(sent, auto_tokenizer)
            sent = add_joint_label(sent, ids)
            print(json.dumps(sent), file=fout)


"""
p data/q_process.py make_label_file
p data/q_process.py make_sentences ../quintuplet/outputs/data/flat/train.json temp/train.json
p data/q_process.py make_sentences ../quintuplet/outputs/data/flat/dev.json temp/dev.json
p data/q_process.py make_sentences ../quintuplet/outputs/data/flat/test.json temp/test.json

p data/q_process.py process temp/train.json data/quintuplet/train.json
p data/q_process.py process temp/dev.json data/quintuplet/dev.json
p data/q_process.py process temp/test.json data/quintuplet/test.json

"""


if __name__ == "__main__":
    fire.Fire()
import json
import pickle
import random
import shutil
from ast import literal_eval
from pathlib import Path
from typing import Dict, List, Tuple

import fire
import numpy as np
from pydantic import BaseModel
from pydantic.main import Extra
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


class Entity(BaseModel):
    emId: str
    text: str
    offset: Span  # Token spans, start inclusive, end exclusive
    label: str

    def as_tuple(self) -> Tuple[int, int, str]:
        return self.offset[0], self.offset[1], self.label


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

    def parse_span(self, i: str) -> Tuple[int, int]:
        x = literal_eval(i)
        if not isinstance(x[0], int):
            return x[0]
        return x

    def parse_relation(self, triplets: List[Relation]) -> str:
        label = ""
        for r in triplets:
            if self.parse_span(r.em1Id) == self.parse_span(self.em1Id):
                if self.parse_span(r.em2Id) == self.parse_span(self.em2Id):
                    label = r.label
        return label

    def as_texts(
        self, tokens: List[str], triplets: List[Relation]
    ) -> Tuple[str, str, str, str, str]:
        head = " ".join(tokens[slice(*self.parse_span(self.em1Id))])
        tail = " ".join(tokens[slice(*self.parse_span(self.em2Id))])
        value = " ".join(tokens[slice(*self.parse_span(self.em3Id))])
        relation = self.parse_relation(triplets)
        return (head, relation, tail, self.label, value)


class SparseCube(BaseModel):
    shape: Tuple[int, int, int]
    entries: List[Tuple[int, int, int, int]]

    def check_equal(self, other):
        assert isinstance(other, SparseCube)
        return self.shape == other.shape and set(self.entries) == set(other.entries)

    @classmethod
    def from_numpy(cls, x: np.ndarray):
        entries = []
        i_list, j_list, k_list = x.nonzero()
        for i, j, k in zip(i_list, j_list, k_list):
            entries.append((i, j, k, x[i, j, k]))
        return cls(shape=tuple(x.shape), entries=entries)

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
    qualifierMentions: List[Qualifier] = []
    wordpieceSentText: str
    wordpieceTokensIndex: List[Span]
    wordpieceSegmentIds: List[int]
    jointLabelMatrix: List[List[int]]
    quintupletMatrix: SparseCube = SparseCube.empty()

    def check_span_overlap(self) -> bool:
        entity_pos = [0 for _ in range(9999)]
        for e in self.entityMentions:
            st, ed = e.offset
            for i in range(st, ed):
                if entity_pos[i] != 0:
                    return True
                entity_pos[i] = 1
        return False

    @property
    def tokens(self) -> List[str]:
        return self.sentText.split(" ")


def load_sents(path: str) -> List[Sentence]:
    with open(path) as f:
        return [Sentence(**json.loads(line)) for line in tqdm(f.readlines(), desc=path)]


def save_sents(sents: List[Sentence], path: str):
    with open(path, "w") as f:
        for s in sents:
            f.write(s.json() + "\n")


class RawPred(BaseModel, extra=Extra.forbid, arbitrary_types_allowed=True):
    tokens: np.ndarray
    span2ent: Dict[Span, str]
    span2rel: Dict[Tuple[Span, Span], int]
    joint_label_matrix: np.ndarray
    joint_label_preds: np.ndarray
    quintuplet_preds: SparseCube = SparseCube.empty()
    separate_positions: List[int]
    all_separate_position_preds: List[int]
    all_ent_preds: Dict[Span, str]
    all_rel_preds: Dict[Tuple[Span, Span], str]
    all_q_preds: Dict[Tuple[Span, Span, Span], str] = {}
    all_rel_probs: Dict[Tuple[Span, Span], float] = {}
    all_q_probs: Dict[Tuple[Span, Span, Span], float] = {}

    def assert_valid(self):
        assert self.tokens.size > 0
        assert self.joint_label_matrix.size > 0
        assert self.joint_label_preds.size > 0

    @classmethod
    def empty(cls):
        return cls(
            tokens=np.array([]),
            span2ent={},
            span2rel={},
            joint_label_matrix=np.empty(shape=(1,)),
            joint_label_preds=np.empty(shape=(1,)),
            separate_positions=[],
            all_separate_position_preds=[],
            all_ent_preds={},
            all_rel_preds={},
        )

    def check_if_empty(self):
        return len(self.tokens) == 0

    def has_relations(self) -> bool:
        return len(self.all_rel_preds.keys()) > 0

    def as_sentence(self, vocab) -> Sentence:
        tokens = [vocab.get_token_from_index(i, "tokens") for i in self.tokens]
        tokens = [t for t in tokens if t != vocab.DEFAULT_PAD_TOKEN]
        text = " ".join(tokens)

        span_to_ent = {}
        for span, label in self.all_ent_preds.items():
            e = Entity(
                emId=str((span, label)),
                offset=span,
                text=" ".join(tokens[slice(*span)]),
                label=label,
            )
            span_to_ent[span] = e

        relations = []
        for (head, tail), label in self.all_rel_preds.items():
            head_id = span_to_ent[head].emId
            tail_id = span_to_ent[tail].emId
            r = Relation(
                em1Id=head_id, em2Id=tail_id, em1Text="", em2Text="", label=label
            )
            relations.append(r)

        qualifiers = []
        for (head, tail, value), label in self.all_q_preds.items():
            q = Qualifier(
                em1Id=span_to_ent[head].emId,
                em2Id=span_to_ent[tail].emId,
                em3Id=span_to_ent[value].emId,
                label=label,
            )
            qualifiers.append(q)

        return Sentence(
            articleId=str((text, relations)),
            sentText=text,
            entityMentions=list(span_to_ent.values()),
            relationMentions=relations,
            qualifierMentions=qualifiers,
            sentId=0,
            wordpieceSentText="",
            wordpieceTokensIndex=[],
            wordpieceSegmentIds=[],
            jointLabelMatrix=[],
        )


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
    is_roberta = "roberta" in type(tokenizer).__name__.lower()
    if is_roberta:
        wordpiece_tokens.pop()  # RoBERTa format is [cls, tokens, sep, pad]

    context_len = len(wordpiece_tokens)
    wordpiece_segment_ids = [0] * context_len

    wordpiece_tokens_index = []
    cur_index = len(wordpiece_tokens)
    for token in sent["sentText"].split(" "):
        if is_roberta:
            token = " " + token  # RoBERTa is space-sensitive
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


def add_joint_label(sent, label_vocab):
    """add_joint_label add joint labels for sentences"""

    ent_rel_id = label_vocab["id"]
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
    for q in sent["qualifierMentions"]:
        for i in range(ent2offset[q["em1Id"]][0], ent2offset[q["em1Id"]][1]):
            for j in range(ent2offset[q["em2Id"]][0], ent2offset[q["em2Id"]][1]):
                for k in range(ent2offset[q["em3Id"]][0], ent2offset[q["em3Id"]][1]):
                    entries.append((i, j, k, ent_rel_id[q["label"]]))

    sent["jointLabelMatrix"] = label_matrix
    sent["quintupletMatrix"] = SparseCube(
        shape=(seq_len, seq_len, seq_len), entries=entries
    ).dict()
    return sent


def add_tag_joint_label(sent, label_vocab):
    ent_rel_id = label_vocab["id"]
    none_id = ent_rel_id["O"]
    seq_len = len(sent["sentText"].split(" "))
    label_matrix = [[none_id for j in range(seq_len)] for i in range(seq_len)]

    spans = [Entity(**e).as_tuple() for e in sent["entityMentions"]]
    encoder = BioEncoder()
    tags = encoder.run(spans, seq_len)
    if not sorted(encoder.decode(tags)) == sorted(spans):
        print(dict(gold=sorted(spans), decoded=sorted(encoder.decode(tags))))

    assert len(tags) == seq_len
    for i, t in enumerate(tags):
        label_matrix[i][i] = ent_rel_id[t]  # We only care about diagonal here

    sent["jointLabelMatrix"] = label_matrix
    sent["quintupletMatrix"] = SparseCube.empty().dict()
    return sent


def process(
    source_file: str,
    target_file: str,
    label_file: str = "data/quintuplet/label_vocab.json",
    pretrained_model: str = "bert-base-uncased",
    mode: str = "",
):
    print(dict(process=locals()))
    auto_tokenizer = AutoTokenizer.from_pretrained(pretrained_model)
    print("Load {} tokenizer successfully.".format(pretrained_model))

    with open(label_file) as f:
        label_vocab = json.load(f)

    with open(source_file) as fin, open(target_file, "w") as fout:
        for line in tqdm(fin.readlines()):
            if mode == "tags":
                s = Sentence(**json.loads(line))
                for s in convert_sent_to_tags(s):
                    sent = s.dict()
                    sent = add_tokens(sent, auto_tokenizer)
                    sent = add_tag_joint_label(sent, label_vocab)
                    print(json.dumps(sent), file=fout)
            else:
                sent = json.loads(line.strip())
                sent = add_tokens(sent, auto_tokenizer)
                if mode == "joint":
                    sent = add_joint_label(sent, label_vocab)
                else:
                    raise ValueError
                print(json.dumps(sent), file=fout)


def make_label_file(pattern_in: str, path_out: str):
    sents = []
    for path in sorted(Path().glob(pattern_in)):
        with open(path) as f:
            sents.extend([Sentence(**json.loads(line)) for line in tqdm(f)])

    relations = sorted(set(r.label for s in sents for r in s.relationMentions))
    qualifiers = sorted(set(q.label for s in sents for q in s.qualifierMentions))
    labels = ["None", "Entity"] + qualifiers + sorted(set(relations) - set(qualifiers))
    label_map = {name: i for i, name in enumerate(labels)}
    print(dict(relations=len(relations), qualifiers=len(qualifiers)))

    info = dict(
        id=label_map,
        symmetric=[],
        asymmetric=[],
        entity=[label_map["Entity"]],
        relation=[label_map[name] for name in relations],
        qualifier=[label_map[name] for name in qualifiers],
        q_num_logits=len(qualifiers) + 2,
    )
    Path(path_out).parent.mkdir(exist_ok=True, parents=True)
    with open(path_out, "w") as f:
        f.write(json.dumps(info, indent=2))


def make_tag_label_file(pattern_in: str, path_out: str):
    tags = []
    qualifiers = []
    for path in sorted(Path().glob(pattern_in)):
        with open(path) as f:
            for line in tqdm(f):
                s = Sentence(**json.loads(line))
                for q in s.qualifierMentions:
                    tags.append("B-" + q.label)
                    tags.append("I-" + q.label)
                    qualifiers.append(q.label)  # Dataset reader needs it

    tags = sorted(set(tags))
    qualifiers = sorted(set(qualifiers))
    labels = ["O"] + tags + qualifiers
    info = dict(
        id={name: i for i, name in enumerate(labels)},
        q_num_logits=len(tags) + 1,
    )
    print(dict(labels=len(labels), tags=len(tags), qualifiers=len(qualifiers)))
    Path(path_out).parent.mkdir(exist_ok=True, parents=True)
    with open(path_out, "w") as f:
        f.write(json.dumps(info, indent=2))


def convert_sent_to_tags(sent: Sentence) -> List[Sentence]:
    id_to_entity = {e.emId: e for e in sent.entityMentions}
    pair_to_qualifiers = {}
    for q in sent.qualifierMentions:
        pair_to_qualifiers.setdefault((q.em1Id, q.em2Id), []).append(q)

    outputs = []
    for r in sent.relationMentions:
        head = id_to_entity[r.em1Id]
        tail = id_to_entity[r.em2Id]
        parts = [sent.sentText, head.text, r.label, tail.text]
        text = " | ".join(parts)
        ents = []
        for q in pair_to_qualifiers.get((r.em1Id, r.em2Id), []):
            e = id_to_entity[q.em3Id].copy(deep=True)
            e.label = q.label
            ents.append(e)

        new = sent.copy(deep=True)
        new.articleId = text
        new.sentText = text
        new.entityMentions = ents
        new.relationMentions = []
        new.qualifierMentions = []
        outputs.append(new)

    return outputs


def load_raw_preds(path: str) -> List[RawPred]:
    raw_preds = []
    with open(path, "rb") as f:
        raw = pickle.load(f)
        for r in raw:
            p = RawPred(**r)
            p.assert_valid()
            raw_preds.append(p)
    return raw_preds


def process_many(
    dir_in: str,
    dir_out: str,
    dir_temp: str = "temp",
    mode: str = "joint",
    **kwargs,
):
    if Path(dir_temp).exists():
        shutil.rmtree(dir_temp)
    for path in sorted(Path(dir_in).glob("*.json")):
        make_sentences(str(path), str(Path(dir_temp) / path.name))

    path_label = str(Path(dir_out) / "label.json")
    if mode == "tags":
        make_tag_label_file("temp/*.json", path_label)
    else:
        make_label_file("temp/*.json", path_label)
    for path in sorted(Path(dir_temp).glob("*.json")):
        process(
            str(path), str(Path(dir_out) / path.name), path_label, mode=mode, **kwargs
        )


def make_labeled_train_split(dir_in: str, dir_out: str, num_train: int, seed: int = 0):
    """Partition the existing dev/test into labeled train/dev/test"""
    if Path(dir_out).exists():
        shutil.rmtree(dir_out)
    shutil.copytree(dir_in, dir_out)
    with open(Path(dir_in) / "dev.json") as f:
        dev = [Sentence(**json.loads(line)) for line in f]
    with open(Path(dir_in) / "test.json") as f:
        test = [Sentence(**json.loads(line)) for line in f]

    random.seed(0)
    total = len(dev) + len(test)
    indices_dev = random.sample(range(len(dev)), k=num_train // 2)
    indices_test = random.sample(range(len(test)), k=num_train // 2)
    train = [dev[i] for i in indices_dev] + [test[i] for i in indices_test]
    dev = [x for i, x in enumerate(dev) if i not in indices_dev]
    test = [x for i, x in enumerate(test) if i not in indices_test]
    assert len(train) + len(dev) + len(test) == total

    for key, sents in dict(train=train, dev=dev, test=test).items():
        print(dict(key=key, sents=len(sents)))
        path = Path(dir_out) / f"{key}.json"
        with open(path, "w") as f:
            for s in sents:
                f.write(s.json() + "\n")


class BioEncoder:
    def run(self, spans: List[Tuple[int, int, str]], length: int) -> List[str]:
        tags = ["O" for _ in range(length)]
        for start, end, label in spans:
            assert start < end
            assert end <= length
            for i in range(start, end):
                tags[i] = "I-" + label
            tags[start] = "B-" + label
        return tags

    def decode(self, tags: List[str]) -> List[Tuple[int, int, str]]:
        parts = []
        for i, t in enumerate(tags):
            assert t[0] in "BIO"
            if t.startswith("B"):
                parts.append([i])
            if parts and t.startswith("I"):
                parts[-1].append(i)

        spans = []
        for indices in parts:
            if indices:
                start = min(indices)
                end = max(indices) + 1
                label = tags[start].split("-", maxsplit=1)[1]
                spans.append((start, end, label))

        return spans


def test_bio():
    encoder = BioEncoder()
    spans = [(0, 3, "one"), (3, 4, "one"), (7, 8, "three")]
    tags = encoder.run(spans, 8)
    preds = encoder.decode(tags)
    print(dict(spans=spans))
    print(dict(tags=tags))
    print(dict(pred=preds))
    assert spans == preds


def analyze_sents(sents: List[Sentence]):
    info = dict(
        sents=len(sents),
        relations=len(set(r.label for s in sents for r in s.relationMentions)),
        qualifiers=len(set(q.label for s in sents for q in s.qualifierMentions)),
    )
    print(json.dumps(info, indent=2))


"""
p data_process.py process_many ../quintuplet/outputs/data/flat_min_10/ data/q10/
p data_process.py process_many ../quintuplet/outputs/data/flat_min_10/ data/q10_tags/ --mode tags

p data_process.py make_sentences ../quintuplet/outputs/data/flat_min_10/pred.json data/q10/gen_pred.json
p data_process.py make_sentences ../quintuplet/outputs/data/flat_min_10/pred_seed_1.json data/q10/gen_1.json
p data_process.py make_sentences ../quintuplet/outputs/data/flat_min_10/pred_seed_2.json data/q10/gen_2.json
p data_process.py make_sentences ../quintuplet/outputs/data/flat_min_10/pred_seed_3.json data/q10/gen_3.json
p data_process.py make_sentences ../quintuplet/outputs/data/flat_min_10/pred_seed_4.json data/q10/gen_4.json

################################################################################

p data_process.py process_many ../quintuplet/outputs/data/flat_min_0/ data/q0/
p data_process.py process_many ../quintuplet/outputs/data/flat_min_0/ data/q0_tags/ --mode tags

################################################################################

"""


if __name__ == "__main__":
    fire.Fire()
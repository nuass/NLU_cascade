#! -*- coding:utf-8 -*-
# bert+ poolout+softmax 意图分类
# bert+crf 实体识别：先识别BIO，再识别对应的分类

import os
os.environ['CUDA_LAUNCH_BLOCKING'] = "1"
import numpy as np
import torch
from torch.utils.data import DataLoader
import torch.nn as nn
import torch.optim as optim
from bert4torch.models import build_transformer_model, BaseModel
from bert4torch.tokenizers import Tokenizer
from bert4torch.snippets import sequence_padding, ListDataset, seed_everything, get_pool_emb
from bert4torch.layers import CRF
from bert4torch.callbacks import Callback,AdversarialTraining

from tqdm import tqdm
import yaml
import re
from sklearn.metrics import accuracy_score, precision_score,f1_score
import sys
import torch.nn.functional as F

maxlen = 256
batch_size = 16

# BERT base
config_path = './chinese_L-12_H-768_A-12/bert4torch_config.json'
checkpoint_path = './chinese_L-12_H-768_A-12/pytorch_model.bin'
dict_path = './chinese_L-12_H-768_A-12/vocab.txt'
device = 'cuda' if torch.cuda.is_available() else 'cpu'
dataset= './data/auto_instructions.yml'

# 固定seed
seed_everything(42)

class RASADataset(ListDataset):
    def __init__(self, file_path=None, data=None, **kwargs):
        self.intent_entity_labels =self.get_labels(file_path)
        super().__init__(file_path, data, **kwargs)

    def extract_entities(self, intent, text):
        # 使用正则表达式匹配文本中的实体和类型
        pattern = r'\[([^\]]+)\]\(([^)]+)\)'
        entities = re.findall(pattern, text)
        results,offset = [],0
        for e in entities:
            text = text.replace("(" + e[1] + ")", '').replace("[", '').replace("]", '').replace("\n", '')
        for entity in entities:
            start = text.find(entity[0], offset)
            end = start + len(entity[0])
            results.append([start, end - 1, entity[1]])
            offset = end

        return [text] + [intent] + results

    def get_labels(self,filename):
        intent_labels=[]
        entity_labels=[]
        regex = r"\((.*?)\)"
        with open(filename, 'r') as file:
            parsed_yaml = yaml.safe_load(file)
            for item in parsed_yaml["nlu"]:
                if "intent" not in item:continue
                intent = item["intent"].split("/")[1]
                if intent not in intent_labels:
                    intent_labels.append(intent)
                examples = item["examples"].split("- ")
                for example in examples:
                    if example.strip()=='':continue
                    extracted_texts = re.findall(regex, example)
                    add_labels = [i for i in extracted_texts if i not in entity_labels]
                    entity_labels.extend(add_labels)
        return (intent_labels,entity_labels)

    def load_data(self, filename):
        data = []
        with open(filename, 'r') as file:
            parsed_yaml = yaml.safe_load(file)
            for item in parsed_yaml["nlu"]:
                if "intent" in item:
                    intent = item["intent"].split("/")[1]
                    examples = item["examples"].split("- ")
                    for example in examples:
                        if example.strip() == '': continue
                        matches = self.extract_entities(intent, example)
                        data.append(list(matches))

        return data


intents_categories,entity_categories = RASADataset(file_path=dataset).intent_entity_labels

# 建立分词器
tokenizer = Tokenizer(dict_path, do_lower_case=True)

def collate_fn(batch):
    batch_token_ids, batch_labels, batch_entity_ids, batch_entity_labels = [], [], [], []
    intent_label_ids = []
    for d in batch:

        tokens = tokenizer.tokenize(d[0], maxlen=maxlen)
        mapping = tokenizer.rematch(d[0], tokens)
        start_mapping = {j[0]: i for i, j in enumerate(mapping) if j}
        end_mapping = {j[-1]: i for i, j in enumerate(mapping) if j}
        token_ids = tokenizer.tokens_to_ids(tokens)
        labels = np.zeros(len(token_ids))
        entity_ids, entity_labels = [], [] #
        for start, end, label in d[2:]:
            if start in start_mapping and end in end_mapping:
                start = start_mapping[start]
                end = end_mapping[end]
                labels[start] = 1 # 标记B
                labels[start + 1:end + 1] = 2 # 标记I
                entity_ids.append([start, end]) 
                entity_labels.append(entity_categories.index(label)+1)

        if not entity_ids:  # 至少要有一个标签
            entity_ids.append([0, 0])  # 如果没有则用0填充
            entity_labels.append(0)

        batch_token_ids.append(token_ids)
        batch_labels.append(labels)#batch_labels 
        batch_entity_ids.append(entity_ids)
        batch_entity_labels.append(entity_labels)

        intent_label=d[1]
        intent_label_ids.append([intents_categories.index(intent_label)])

    batch_token_ids = torch.tensor(sequence_padding(batch_token_ids), dtype=torch.long, device=device)
    batch_labels = torch.tensor(sequence_padding(batch_labels), dtype=torch.long, device=device)
    batch_entity_ids = torch.tensor(sequence_padding(batch_entity_ids), dtype=torch.long, device=device)  # [btz, 实体个数，start/end]
    batch_entity_labels = torch.tensor(sequence_padding(batch_entity_labels), dtype=torch.long, device=device)  # [btz, 实体个数]

    intent_label_ids = torch.tensor(intent_label_ids, dtype=torch.long, device=device)

    return [batch_token_ids, batch_entity_ids], [batch_labels, batch_entity_labels,intent_label_ids]

# 转换数据集

train_dataloader = DataLoader(RASADataset('./data/auto_instructions.yml'), batch_size=batch_size, shuffle=True,collate_fn=collate_fn) 
valid_dataloader = DataLoader(RASADataset('./data/auto_instructions.yml'), batch_size=batch_size, collate_fn=collate_fn) 

# 定义bert上的模型结构
class Model(BaseModel):
    def __init__(self):
        super().__init__()
        self.bert = build_transformer_model(config_path=config_path, checkpoint_path=checkpoint_path, segment_vocab_size=0, with_pool=True)
        # 意图分类参数
        self.dense4intent = nn.Linear(self.bert.configs['hidden_size'], len(intents_categories))
        # 实体参数
        self.dense4crf = nn.Linear(768, len(entity_categories))
        self.dense4entity = nn.Linear(768, len(entity_categories)+1)  # 包含padding
        self.crf = CRF(len(entity_categories))

        self.dropout = nn.Dropout(0.1)

    def forward(self, *inputs):
        token_ids, entity_ids = inputs[0], inputs[1]
        last_hidden_state, pooled_output = self.bert([token_ids])  # [btz, seq_len, hdsz]
        output = self.dropout(pooled_output)

        # 意图分类输出
        output = self.dense4intent(output)

        # 实体识别一阶段的输出

        emission_score = self.dense4crf(last_hidden_state)  # [bts, seq_len, tag_size]
        attention_mask = token_ids.gt(0)

        # 实体识别阶段输出
        btz, entity_count, _ = entity_ids.shape
        hidden_size = last_hidden_state.shape[-1]
        entity_ids = entity_ids.reshape(btz, -1, 1).repeat(1, 1, hidden_size)
        entity_states = torch.gather(last_hidden_state, dim=1, index=entity_ids).reshape(btz, entity_count, -1, hidden_size)
        entity_states = torch.mean(entity_states, dim=2)  # 取实体首尾hidden_states的均值
        entity_logit = self.dense4entity(entity_states)  # [btz, 实体个数，实体类型数]

        return emission_score, attention_mask, entity_logit,output

    def predict(self, token_ids):
        self.eval()
        with torch.no_grad():
            last_hidden_state, pooled_output = self.bert([token_ids])  # [btz, seq_len, hdsz]

            output = self.dense4intent(pooled_output)
            # 意图（分类）
            intent_pred = torch.argmax(output, dim=-1) 
            intent_prob = F.softmax(output, dim=-1)
            intent_prob, _ = torch.max(intent_prob, dim=-1)

            emission_score = self.dense4crf(last_hidden_state)  # [bts, seq_len, tag_size]
            attention_mask = token_ids.gt(0)
            best_path = self.crf.decode(emission_score, attention_mask)  # [bts, seq_len]

            # 实体抽取
            batch_entity_ids = []
            for one_samp in best_path:
                entity_ids = []
                for j, item in enumerate(one_samp):
                    if item.item() == 1:  # B
                        entity_ids.append([j, j])
                    elif len(entity_ids) == 0:
                        continue
                    elif (len(entity_ids[-1]) > 0) and (item.item() == 2):  # I
                        entity_ids[-1][-1] = j
                    elif len(entity_ids[-1]) > 0:
                        entity_ids.append([])
                if not entity_ids:  # 至少要有一个标签
                    entity_ids.append([0, 0])  # 如果没有则用0填充
                batch_entity_ids.append([i for i in entity_ids if i])
            batch_entity_ids = torch.tensor(sequence_padding(batch_entity_ids), dtype=torch.long, device=device)  # [btz, 实体个数，start/end]

            btz, entity_count, _ = batch_entity_ids.shape
            hidden_size = last_hidden_state.shape[-1]
            gather_index = batch_entity_ids.reshape(btz, -1, 1).repeat(1, 1, hidden_size)
            entity_states = torch.gather(last_hidden_state, dim=1, index=gather_index).reshape(btz, entity_count, -1, hidden_size)
            entity_states = torch.mean(entity_states, dim=2)  # 取实体首尾hidden_states的均值
            entity_logit = self.dense4entity(entity_states)  # [btz, 实体个数，实体类型数]
            entity_prob = F.softmax(entity_logit, dim=-1)
            entity_prob, _ = torch.max(entity_prob, dim=-1)

            entity_pred = torch.argmax(entity_logit, dim=-1)  # [btz, 实体个数]

            # 每个元素为一个三元组
            entity_tulpe = trans_entity2tuple(batch_entity_ids, entity_pred)

        return best_path, (intent_pred,intent_prob),(entity_tulpe,entity_prob),

model = Model().to(device)

class Loss(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        # 意图损失
        self.loss4intent = nn.CrossEntropyLoss()

        self.loss4entity = nn.CrossEntropyLoss(ignore_index=0)

    def forward(self, outputs, labels):
        emission_score, attention_mask, entity_logit,intent_logit = outputs

        seq_labels, entity_labels,intent_labels = labels
        # 意图损失
        loss_i = self.loss4intent(intent_logit.reshape(-1, intent_logit.shape[-1]), intent_labels.flatten())

        # loss1 loss2 均为实体阶段损失
        loss_e1 = model.crf(emission_score, attention_mask, seq_labels)
        loss_e2 = self.loss4entity(entity_logit.reshape(-1, entity_logit.shape[-1]), entity_labels.flatten())

        return {'loss': (loss_e1+loss_e2+loss_i)/3, 'loss_e1': loss_e1, 'loss_e2': loss_e2, 'loss_i': loss_i}

# Loss返回的key会自动计入metrics，下述metrics不写仍可以打印loss1和loss2
model.compile(loss=Loss(), optimizer=optim.Adam(model.parameters(), lr=2e-5))

def evaluate(data):
    X1, Y1, Z1 = 1e-10, 1e-10, 1e-10
    X2, Y2, Z2 = 1e-10, 1e-10, 1e-10

    intentLabels =[]
    intentPreds = []
    for (token_ids, entity_ids), (label, entity_labels,intent_labels) in tqdm(data):
        scores, (intent_pred,_),(entity_pred,_) = model.predict(token_ids)  # [btz, seq_len]

        # 意图分类统计
        intentLabels+=intent_labels.flatten().tolist()
        intentPreds+=intent_pred.tolist()

        # 一阶段指标: token粒度
        # attention_mask = label.gt(0)
        # X1 += (scores.eq(label) * attention_mask).sum().item()
        # Y1 += scores.gt(0).sum().item()
        # Z1 += label.gt(0).sum().item()

        # 二阶段指标：entity粒度
        entity_true = trans_entity2tuple(entity_ids, entity_labels)
        X2 += len(entity_pred.intersection(entity_true))
        Y2 += len(entity_pred)
        Z2 += len(entity_true)

    intent_accuracy = accuracy_score(intentLabels, intentPreds)

    # 意图指标
    intent_f1 = f1_score(intentLabels, intentPreds, average='macro')

    #f1, precision, recall = 2 * X1 / (Y1 + Z1), X1 / Y1, X1 / Z1
    f2, precision2, recall2 = 2 * X2 / (Y2 + Z2), X2/ Y2, X2 / Z2

    return  f2, precision2, recall2,intent_accuracy,intent_f1

def trans_entity2tuple(entity_ids, entity_labels):
    '''把tensor转为(样本id, start, end, 实体类型)的tuple用于计算指标
    '''
    entity_true = set()
    for i, one_sample in enumerate(entity_ids):
        for j, item in enumerate(one_sample):
            if item[0].item() * item[1].item() != 0:
                entity_true.add((i, item[0].item(), item[1].item(), entity_labels[i, j].item()))
    return entity_true

class Evaluator(Callback):
    """评估与保存
    """
    def __init__(self):
        self.best_val_f1 = 0.

    def on_epoch_end(self, steps, epoch, logs=None):

        f2, precision2, recall2,intent_acc,intgent_f1 = evaluate(valid_dataloader)
        if (f2+intgent_f1)/2 > self.best_val_f1:
            self.best_val_f1 = (f2+intgent_f1)/2
            model.save_weights('./result/best_model.pt')

        print(f'[实体识别] f1: {f2:.5f}, p: {precision2:.5f} r: {recall2:.5f}\n')
        print(f'[意图识别] f1: {intgent_f1:.5f}, acc: {intent_acc:.5f}\n')
        print(f'[整体均值] best_f1: {self.best_val_f1:.5f}\n')


if __name__ == '__main__' and 'train' in sys.argv:

    evaluator = Evaluator()
    adversarial_train = AdversarialTraining('fgm')
    model.fit(train_dataloader, epochs=20, steps_per_epoch=None, callbacks=[evaluator,adversarial_train])

else:

    print(intents_categories,entity_categories)
    text = "导航去新浪总部"
    model.load_weights('./result/best_model.pt')

    tokens = tokenizer.tokenize(text, maxlen=maxlen)

    token_ids_batch = tokenizer.tokens_to_ids(tokens)

    batch_token_ids = torch.tensor(sequence_padding([token_ids_batch]), dtype=torch.long, device=device)

    _,  intent_pred_prob,entity_pred_prob = model.predict(batch_token_ids)
    # 意图id2label及置信度
    print("意图：",intents_categories[intent_pred_prob[0]],intent_pred_prob[1].tolist()[0])
    # 实体id2label及置信度
    entities=[]
    entity_prob = entity_pred_prob[1].tolist()[0]

    for i,e in enumerate(entity_pred_prob[0]):
        entities.append({"entity": text[e[1] - 1:e[2]], "type": entity_categories[e[3] - 1],"score":entity_prob[i]})
    print("实体：",entities)


# coding=utf-8
# Copyright (c) 2019, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""utils for creating datasets"""
import os
import math
import time
import random
import torch

from .samplers import DistributedBatchSampler
from .datasets import split_ds, ConcatDataset, SplitDataset, BertSentencepairDataset, \
    GPT2Dataset, ShuffleDataset, XLDataset, BlockDataset
from .lazy_loader import exists_lazy, LazyWriter, LazyLoader
from .tokenization import Tokenization, CommandToken, Tokenizer, CharacterLevelTokenizer, BertWordPieceTokenizer, \
    GPT2BPETokenizer, make_tokenizer
from . import corpora

TRAIN_DATA = 0
VAL_DATA = 1
TEST_DATA = 2


def should_split(split):
    """
    given split proportions checks if should split
    Examples:
    >>> should_split([10,0,0]) 
    False
    >>> should_split([1,.1,.2])
    True
    """
    return max(split) / sum(split) != 1.


def get_ext(path):
    """gets path extension"""
    return os.path.splitext(path)[1]


def get_dataset(name, tokenizer, pre_tokenize):
    """gets dataset object based on keyword args and file at `path`"""
    if supported_corpus(name):
        dataset = corpora.NAMED_CORPORA[name]
        path = dataset.PATH
        if issubclass(dataset, corpora.PromptReader):
            if not (exists_lazy(path, data_type='prompt') and exists_lazy(path, data_type='text')):
                # create cached version of dataset for lazy loading if it doesn't exist
                if torch.distributed.get_rank() == 0:
                    prompt_writer = LazyWriter(path, data_type='prompt', is_array=pre_tokenize)
                    text_writer = LazyWriter(path, data_type='text', is_array=pre_tokenize)
                    writers = {'prompt': prompt_writer, 'text': text_writer}
                    dataset(writers=writers, tokenizer=tokenizer, tokenize=pre_tokenize)
                    prompt_writer.close()
                    text_writer.close()
                else:
                    while not os.path.exists(LazyWriter.get_len_path(path, data_type='prompt')):
                        time.sleep(1)
            map_fn = (lambda x: x.tolist()) if pre_tokenize else None
            prompts = LazyLoader(path, data_type='prompt', map_fn=map_fn, mem_map=True,
                                 is_array=pre_tokenize)
            texts = LazyLoader(path, data_type='text', map_fn=map_fn, mem_map=True,
                               is_array=pre_tokenize)
            text = corpora.PromptDataset(prompt_loader=prompts, text_loader=texts, tokenizer=tokenizer,
                                         to_tokenize=not pre_tokenize)
            if torch.distributed.get_rank() == 0:
                print(f"Create dataset {name} with {len(text)} documents")
                for i in range(10):
                    rand_id = i if i < 5 else random.randrange(len(text))
                    sample_tokens = text[rand_id]['tokens'][:1024]
                    print(sample_tokens)
                    print(tokenizer.DecodeIds(sample_tokens).encode('utf-8'))
            return text
        elif issubclass(dataset, corpora.KeyReader):
            if not (exists_lazy(path, data_type='text') and exists_lazy(path, data_type='mask')):
                # create cached version of dataset for lazy loading if it doesn't exist
                if torch.distributed.get_rank() == 0:
                    text_writer = LazyWriter(path, data_type='text', is_array=pre_tokenize)
                    mask_writer = LazyWriter(path, data_type='mask', is_array=True)
                    writers = {'mask': mask_writer, 'text': text_writer}
                    dataset(writers=writers, tokenizer=tokenizer, tokenize=pre_tokenize)
                    mask_writer.close()
                    text_writer.close()
                else:
                    while not os.path.exists(LazyWriter.get_len_path(path, data_type='mask')):
                        time.sleep(1)
            map_fn = (lambda x: x.tolist()) if pre_tokenize else None
            masks = LazyLoader(path, data_type='mask', map_fn=map_fn, mem_map=True, is_array=True)
            texts = LazyLoader(path, data_type='text', map_fn=map_fn, mem_map=True, is_array=pre_tokenize)
            text = corpora.KeyDataset(mask_loader=masks, text_loader=texts, tokenizer=tokenizer,
                                      to_tokenize=not pre_tokenize)
            return text
    else:
        raise NotImplementedError('dataset %s is not supported' % name)


def supported_corpus(corpus_name):
    """checks if corpus name is defined in `corpora.py`"""
    return corpus_name in corpora.NAMED_CORPORA


def make_dataset(path, seq_length, mem_length, shuffle=True, split=None, tokenizer=None,
                 sample_one_document=False, pre_tokenize=False, ds_type='', save_splits=None, load_splits=None,
                 save_test_data=None, **kwargs):
    """function to create datasets+tokenizers for common options"""
    if split is None:
        split = [1.]

    # get one or multiple datasets and concatenate
    if isinstance(path, str):
        ds = get_dataset(path, tokenizer=tokenizer, pre_tokenize=pre_tokenize)
    else:
        ds = [get_dataset(p, tokenizer=tokenizer, pre_tokenize=pre_tokenize) for p in path]
        ds = ConcatDataset(ds)

    # Split dataset into train/val/test (and wrap bert dataset)
    def wrap_dataset(dataset):
        if ds_type.lower() == 'bert':
            presplit_sentences = kwargs['presplit_sentences'] if 'presplit_sentences' in kwargs else False
            dataset = BertSentencepairDataset(dataset, max_seq_len=seq_length, presplit_sentences=presplit_sentences)
        elif ds_type.lower() == 'gpt-xl':
            assert pre_tokenize
            dataset = XLDataset(dataset, tokenizer, max_seq_len=seq_length, mem_len=mem_length,
                                sample_across_doc=not sample_one_document)
        elif ds_type.lower() == 'gpt2':
            dataset = GPT2Dataset(dataset, tokenizer, max_seq_len=seq_length, sample_across_doc=not sample_one_document)
        elif ds_type.lower() == 'block':
            dataset = BlockDataset(dataset, tokenizer, max_seq_len=seq_length,
                                   sample_across_doc=not sample_one_document)
        return dataset

    if should_split(split):
        ds = split_ds(ds, split, shuffle=shuffle, save_splits=save_splits, load_splits=load_splits)
        if save_test_data is not None and torch.distributed.get_rank() == 0:
            test_ds = ds[-1]
            with open(save_test_data, "w", encoding='utf-8') as output:
                for data in test_ds:
                    text = data['tokens']
                    text = tokenizer.DecodeIds(text)
                    output.write(text)
                    output.write("\n")
            print(f"Write test data to {save_test_data}")
        ds = [wrap_dataset(d) if d is not None else None for d in ds]
    else:
        ds = wrap_dataset(ds)
    return ds

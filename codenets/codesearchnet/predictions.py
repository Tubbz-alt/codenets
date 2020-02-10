#!/usr/bin/env python3
"""
Usage:
    train.py [options] SAVE_FOLDER TRAIN_DATA_PATH VALID_DATA_PATH TEST_DATA_PATH
    train.py [options] [SAVE_FOLDER]

*_DATA_PATH arguments may either accept (1) directory filled with .jsonl.gz files that we use as data,
or a (2) plain text file containing a list of such directories (used for multi-language training).

In the case that you supply a (2) plain text file, all directory names must be separated by a newline.
For example, if you want to read from multiple directories you might have a plain text file called
data_dirs_train.txt with the below contents:

> cat ~/src/data_dirs_train.txt
azure://semanticcodesearch/pythondata/Processed_Data/jsonl/train
azure://semanticcodesearch/csharpdata/split/csharpCrawl-train

Options:
    -h --help                        Show this screen.
    --restore DIR                    specify restoration dir. [optional]
    --debug                          Enable debug routines. [default: False]
"""

import os
from pathlib import Path
from typing import Tuple
import torch
import numpy as np
from docopt import docopt
from dpu_utils.utils import run_and_debug
from loguru import logger
import pandas as pd
from annoy import AnnoyIndex
from tqdm import tqdm

from codenets.codesearchnet.training_ctx import CodeSearchTrainingContext


def compute_code_encodings_from_defs(
    language: str, training_ctx: CodeSearchTrainingContext, lang_token: str, batch_length: int = 1024
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    logger.info(f"Computing Encoding for language: {language}")
    lang_id = training_ctx.train_data_params.lang_ids[language]
    h5_file = (
        training_ctx.pickle_path
        / f"{language}_{training_ctx.training_tokenizer_type}_dedupe_definitions_v2_codes_encoded.h5"
    )
    root_data_path = Path(training_ctx.conf["dataset.root_dir"])

    def_file = root_data_path / f"data/{language}_dedupe_definitions_v2.pkl"
    definitions_df = pd.DataFrame(pd.read_pickle(open(def_file, "rb"), compression=None))
    if not os.path.exists(h5_file):
        logger.info(f"Building encodings of code from {def_file}")

        function_tokens = definitions_df["function_tokens"]
        # add language and lang_token (<lg>) to tokens
        function_tokens = function_tokens.apply(lambda row: [language, lang_token] + row)
        function_tokens_batch = function_tokens.groupby(np.arange(len(function_tokens)) // batch_length)

        code_embeddings = []
        for g, df_batch in tqdm(function_tokens_batch):
            # logger.debug(f"df_batch {df_batch.values}")
            codes_encoded, codes_masks = training_ctx.tokenize_code_tokens(
                df_batch.values, max_length=training_ctx.conf["dataset.common_params.code_max_num_tokens"]
            )

            codes_encoded_t = torch.tensor(codes_encoded, dtype=torch.long).to(training_ctx.device)
            codes_masks_t = torch.tensor(codes_masks, dtype=torch.long).to(training_ctx.device)

            # logger.debug(f"codes_encoded_t {codes_encoded_t}")
            # logger.debug(f"codes_masks_t {codes_masks_t}")

            emb_df = pd.DataFrame(
                training_ctx.encode_code(lang_id=lang_id, code_tokens=codes_encoded_t, code_tokens_mask=codes_masks_t)
                .cpu()
                .numpy()
            )
            # logger.debug(f"codes_encoded_t:{codes_encoded_t.shape} codes_masks_t:{codes_masks_t.shape}")
            if g < 2:
                logger.debug(f"emb_df {emb_df.head()}")
            code_embeddings.append(emb_df)

        code_embeddings_df = pd.concat(code_embeddings)

        logger.debug(f"code_embeddings_df {code_embeddings_df.head(20)}")

        code_embeddings_df.to_hdf(h5_file, key="code_embeddings_df", mode="w")
        return (code_embeddings_df, definitions_df)
    else:
        code_embeddings_df = pd.read_hdf(h5_file, key="code_embeddings_df")
        return (code_embeddings_df, definitions_df)


def run(args, tag_in_vcs=False) -> None:
    os.environ["WANDB_MODE"] = "dryrun"

    logger.debug("Building Training Context")
    training_ctx: CodeSearchTrainingContext
    restore_dir = args["--restore"]
    logger.info(f"Restoring Training Context from directory{restore_dir}")
    training_ctx = CodeSearchTrainingContext.build_context_from_dir(restore_dir)

    queries = pd.read_csv(training_ctx.queries_file)
    queries = list(queries["query"].values)
    queries_tokens, queries_masks = training_ctx.tokenize_query_sentences(
        queries, max_length=training_ctx.conf["dataset.common_params.query_max_num_tokens"]
    )
    logger.info(f"queries_tokens: {queries_tokens}")

    training_ctx.eval_mode()
    with torch.no_grad():
        query_embeddings = (
            training_ctx.encode_query(
                query_tokens=torch.tensor(queries_tokens, dtype=torch.long).to(training_ctx.device),
                query_tokens_mask=torch.tensor(queries_masks, dtype=torch.long).to(training_ctx.device),
            )
            .cpu()
            .numpy()
        )
        logger.info(f"query_embeddings: {query_embeddings.shape}")

        topk = 100
        predictions = []
        language_token = "<lg>"
        for language in ("python",):  # , "go", "javascript", "java", "php", "ruby"):
            # (codes_encoded_df, codes_masks_df, definitions) = get_language_defs(language, training_ctx, language_token)

            code_embeddings, definitions = compute_code_encodings_from_defs(language, training_ctx, language_token)
            logger.debug(f"definitions {definitions.iloc[0]}")
            logger.debug(f"code_embeddings {code_embeddings.values[:5]}")
            logger.info(f"Building Annoy Index of length {len(code_embeddings.values[0])}")
            # indices: AnnoyIndex = AnnoyIndex(code_embeddings[0][0].shape[0], "angular")
            indices: AnnoyIndex = AnnoyIndex(len(code_embeddings.values[0]), "angular")
            # idx = 0
            for index, emb in enumerate(tqdm(code_embeddings.values)):
                # logger.info(f"vectors {vectors}")
                # for vector in vectors:
                # if vector is not None:
                # if idx < 10:
                # logger.debug(f"vector {len(vector)}")
                # indices.add_item(idx, vector)
                # idx += 1
                indices.add_item(index, emb)
            indices.build(10)

            for i, (query, query_embedding) in enumerate(tqdm(zip(queries, query_embeddings))):
                idxs, distances = indices.get_nns_by_vector(query_embedding, topk, include_distances=True)
                if i < 5:
                    logger.debug(f"query_embedding {query_embedding}")
                    logger.debug(f"idxs:{idxs}, distances:{distances}")
                for idx2, _ in zip(idxs, distances):
                    predictions.append(
                        (query, language, definitions.iloc[idx2]["identifier"], definitions.iloc[idx2]["url"])
                    )

            logger.info(f"predictions {predictions[0]}")
            del code_embeddings
            del definitions

    df = pd.DataFrame(predictions, columns=["query", "language", "identifier", "url"])
    df.to_csv(training_ctx.output_dir / f"predictions_{training_ctx.training_tokenizer_type}.csv", index=False)


if __name__ == "__main__":
    args = docopt(__doc__)
    run_and_debug(lambda: run(args), args["--debug"])
import argparse
import os

import torch
from transformers import AutoConfig, AutoModelForSpeechSeq2Seq, AutoModelForCTC, AutoProcessor, MODEL_FOR_SPEECH_SEQ_2_SEQ_MAPPING
import evaluate
from normalizer import data_utils
import time
from tqdm import tqdm

wer_metric = evaluate.load("wer")


def main(args):
    config = AutoConfig.from_pretrained(args.model_id)
    cls_model = AutoModelForSpeechSeq2Seq if type(config) in MODEL_FOR_SPEECH_SEQ_2_SEQ_MAPPING else AutoModelForCTC
    model = cls_model.from_pretrained(args.model_id, torch_dtype=torch.float16).to(args.device)
    processor = AutoProcessor.from_pretrained(args.model_id)
    model_input_name = processor.model_input_names[0]

    if model.can_generate():
        gen_kwargs = {"max_new_tokens": 256}
        # for multilingual Whisper-checkpoints we see a definitive WER boost by setting the language and task args
        if getattr(model.generation_config, "is_multilingual"):
            gen_kwargs["language"] = "en"
            gen_kwargs["task"] = "transcribe"

    dataset = data_utils.load_data(args)

    if args.max_eval_samples is not None and args.max_eval_samples > 0:
        print(f"Subsampling dataset to first {args.max_eval_samples} samples!")
        dataset = dataset.take(args.max_eval_samples)

    dataset = data_utils.prepare_data(dataset)

    def benchmark(batch):
        # Load audio inputs
        audios = [audio["array"] for audio in batch["audio"]]
        minibatch_size = len(audios)

        # START TIMING
        start_time = time.time()

        # 1. Pre-Processing
        if not model.can_generate() or len(audios[0]) > processor.feature_extractor.n_samples:
            # 1.1 Either CTC pre-processing (normalize to mean 0, std 1), or long-form Whisper processing
            inputs = processor(
                audios,
                sampling_rate=16_000,
                truncation=False,
                padding="longest",
                return_tensors="pt",
                return_attention_mask=True,
            )
        else:
            # 1.2 Standard Whisper processing: pad audios to 30-seconds and converted to log-mel
            inputs = processor(audios, sampling_rate=16_000, return_tensors="pt")

        inputs = inputs.to(args.device)
        inputs[model_input_name] = inputs[model_input_name].to(torch.float16)

        # 2. Model Inference
        if model.can_generate():
            # 2.1 Auto-regressive generation for encoder-decoder models
            pred_ids = model.generate(**inputs, **gen_kwargs)
        else:
            # 2.2. Single forward pass for CTC
            with torch.no_grad():
                logits = model(**inputs)
                pred_ids = logits.argmax(-1)

        # 3. Post-processing: convert token ids to text transcription
        pred_text = processor.batch_decode(pred_ids, skip_special_tokens=True)

        # END TIMING
        runtime = time.time() - start_time

        # normalize by minibatch size since we want the per-sample time
        batch["transcription_time_s"] = minibatch_size * [runtime / minibatch_size]

        # normalize transcriptions with English normalizer
        batch["predictions"] = [data_utils.normalizer(pred) for pred in pred_text]
        batch["references"] = batch["norm_text"]
        return batch

    dataset = dataset.map(
        benchmark, batch_size=args.batch_size, batched=True, remove_columns=["audio"]
    )

    all_results = {
        "audio_length_s": [],
        "transcription_time_s": [],
        "predictions": [],
        "references": [],
    }
    result_iter = iter(dataset)
    for result in tqdm(result_iter, desc="Samples"):
        for key in all_results:
            all_results[key].append(result[key])

    # Write manifest results (WER and RTFX)
    manifest_path = data_utils.write_manifest(
        all_results["references"],
        all_results["predictions"],
        args.model_id,
        args.dataset_path,
        args.dataset,
        args.split,
        audio_length=all_results["audio_length_s"],
        transcription_time=all_results["transcription_time_s"],
    )
    print("Results saved at path:", os.path.abspath(manifest_path))

    wer = wer_metric.compute(
        references=all_results["references"], predictions=all_results["predictions"]
    )
    wer = round(100 * wer, 2)
    rtfx = round(sum(all_results["audio_length_s"]) / sum(all_results["transcription_time_s"]), 2)
    print("WER:", wer, "%", "RTFx:", rtfx)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--model_id",
        type=str,
        required=True,
        help="Model identifier. Should be loadable with 🤗 Transformers",
    )
    parser.add_argument(
        "--dataset_path",
        type=str,
        default="esb/datasets",
        help="Dataset path. By default, it is `esb/datasets`",
    )
    parser.add_argument(
        "--dataset",
        type=str,
        required=True,
        help="Dataset name. *E.g.* `'librispeech_asr` for the LibriSpeech ASR dataset, or `'common_voice'` for Common Voice. The full list of dataset names "
        "can be found at `https://huggingface.co/datasets/esb/datasets`",
    )
    parser.add_argument(
        "--split",
        type=str,
        default="test",
        help="Split of the dataset. *E.g.* `'validation`' for the dev split, or `'test'` for the test split.",
    )
    parser.add_argument(
        "--device",
        type=int,
        default=-1,
        help="The device to run the pipeline on. -1 for CPU (default), 0 for the first GPU and so on.",
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        default=16,
        help="Number of samples to go through each streamed batch.",
    )
    parser.add_argument(
        "--max_eval_samples",
        type=int,
        default=None,
        help="Number of samples to be evaluated. Put a lower number e.g. 64 for testing this script.",
    )
    parser.add_argument(
        "--no-streaming",
        dest="streaming",
        action="store_false",
        help="Choose whether you'd like to download the entire dataset or stream it during the evaluation.",
    )
    args = parser.parse_args()
    parser.set_defaults(streaming=False)

    main(args)

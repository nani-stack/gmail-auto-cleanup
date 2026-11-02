# Training a local model on your own mail

The hosted model's judgements are logged on every run, so your `triage_*.log`
files are a labelled corpus. This distils them into a local Laya checkpoint:
the hosted model is the teacher, your local model is the student, and the
targets are the teacher's probabilities rather than hard yes/no labels.

Useful when API credits run out, when the mailbox must stay on your own
hardware, or when you want the questions tuned to one specific inbox.

## 1. Build the dataset

On the machine that has the logs:

```bash
python tools/build_dataset.py --logs ~/path/to/logs --out data/
```

This writes `data/train.jsonl`, `data/val.jsonl` and `data/questions.json` in
the `{state, questions, gold}` schema `laya-train` reads. Each labelled
question becomes one training item, and `gold` carries the teacher's
probability, so the student learns *"the teacher said 0.84"*, not just *"yes"*.

The split is by **sender hash**, not at random: the same sender never appears
in both halves. Splitting randomly inflates the score, because a model that
memorises `nike.com` looks like a model that understands marketing.

**This dataset is your mail.** Sender and subject lines for every message.
Keep it off GitHub and off shared drives; both repos ignore `*.jsonl` and
`data/` already. Move it to the training machine the way you would move any
private data.

### What the logs do not contain

Logs record sender, subject and the probabilities — not the preview text the
hosted model saw. So the student is trained on strictly less information than
the teacher had, which puts a ceiling on how closely it can agree. Keep the
inference-time state to the same two fields, so training and serving match.

## 2. Set up the training machine

A single modern GPU is plenty; the checkpoint is 322M–421M parameters.

```bash
python3 -m venv .venv && . .venv/bin/activate
# Blackwell cards (RTX 50-series) need a CUDA 12.8+ build of PyTorch:
pip install torch --index-url https://download.pytorch.org/whl/cu128
pip install laya
python -c "import torch; print(torch.cuda.get_device_name(0))"
```

With more than one GPU, pin it to one unless you need both:

```bash
export CUDA_VISIBLE_DEVICES=0
```

## 3. Measure the baseline first

Score the stock checkpoint before training, so you know what the finetune
actually bought:

```bash
python tools/evaluate.py --model convaiinnovations/laya --data data/val.jsonl
```

Read precision and recall per question, not accuracy: a question with 1%
positives scores 99% by answering "no" every time. Brier score is the mean
squared error against the teacher's probability, so lower is better. The last
line is the number that matters — how often the whole pipeline would take the
same action as the hosted model.

## 4. Train

```bash
laya-train \
  --data data/train.jsonl \
  --out out/inbox-v1 \
  --base convaiinnovations/laya \
  --loss soft-ce \
  --epochs 3 \
  --micro-batch 32 \
  --grad-accum 2 \
  --encoder-lr 2.5e-5
```

`--loss soft-ce` is the one flag not to change: it trains against the
teacher's probability distribution. The default `--epochs 4` with
`--micro-batch 8 --grad-accum 8` matches the published notebook; the values
above give the same effective batch of 64 with fewer, larger steps.

Check the data before spending GPU time:

```bash
python -c "from laya.train import dry_run; print(dry_run('data/train.jsonl', 'convaiinnovations/laya'))"
```

### If it overfits or underfits

- **Rare questions barely move.** With a few hundred positives out of 18,000,
  one question can be drowned out. Train it alone — filter the JSONL to rows
  labelling that question — or drop it and keep that rule as a sender list,
  which costs nothing and never misfires.
- **Validation agreement falls after epoch 2.** Lower `--epochs`, or add
  `--freeze-encoder` to train only the heads.
- **The model agrees on everything but the rare classes.** That is the
  imbalance, not the model. Compare precision and recall before and after.

## 5. Evaluate, then wire it in

```bash
python tools/evaluate.py --model out/inbox-v1 --data data/val.jsonl
```

If it earns its place, point the config at it:

```yaml
backend: laya
laya:
  model: out/inbox-v1
  batch_size: 32
```

Then **re-tune your thresholds**. The finetuned model's probabilities are its
own; `keep_at` and `cleanup_below` were set for the hosted model. Dry-run over
a few hundred emails, read the log, and adjust before letting it archive
anything.

## What good looks like

The honest bar is not "matches the teacher everywhere". It is:

- **No KEEP becoming CLEANUP** on the validation set. Archiving something that
  should have stayed is the only expensive error; the reverse just leaves mail
  in the inbox.
- **Agreement above the stock checkpoint** by a clear margin, otherwise the
  finetune was not worth running.
- **Probabilities that separate.** If they cluster near 0.5, the thresholds
  have nothing to bite on, whatever the accuracy says.

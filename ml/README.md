## ML training (quick start)

### 1) Install deps

```bash
pip install -r requirements.txt
```

### 2) Export dataset

In Telegram run `/exportml` to generate:
- `docs/ml_dataset_clean2.json` (recommended for training WIN/LOSS model)

### 3) Train baseline model

```bash
python ml_train.py
```

Outputs:
- `ml/model.joblib` (trained pipeline)
- `ml/model_meta.json` (feature list + metrics)


# Apple Foundation Model experiment

This standalone Swift command-line bridge evaluates Apple's Foundation Models on transaction descriptions without connecting the model to the local-finance-tracker importer or writing to the database. It supports the on-device general and content-tagging models and Apple's Private Cloud Compute (PCC) model. The evaluator limits choices to categories represented in the training split, adds transaction-specific category guidance, and can retrieve nearby labelled examples.

The Python evaluation command uses a chronological split and reads the SQLite database read-only. Before either model is used, it applies exact transaction lookup and a conservative cleaned-merchant lookup (at least two training examples with 95% agreement). It then compares Apple's predictions with local-finance-tracker's TF-IDF character n-gram plus logistic-regression classifier on the same remaining held-out rows. It reports model-only and full-pipeline accuracy, macro F1, per-category precision/recall, common confusions, and latency. A row-by-row review CSV is written under ignored `data/`.

Transaction descriptions and the selected few-shot examples are sent to the Swift process. For `--model pcc`, those prompt contents leave the Mac and are sent to Apple's Private Cloud Compute service. The evaluation script opens the SQLite database read-only. It does not update categories, training examples, or imported transactions.

## Requirements

- Apple silicon Mac with macOS 27 or newer.
- Apple Intelligence enabled and the Foundation Model available on the Mac.
- Xcode 27 or newer with the macOS 27 SDK and Swift 6.2+.
- PCC use requires Apple's managed `com.apple.developer.private-cloud-compute` entitlement on the signed executable.
- local-finance-tracker's Python environment and labelled training examples in its local SQLite database.

Check runtime availability and run one real inference (the check classifies a synthetic rent transaction):

```bash
cd "/path/to/local-finance-tracker"
swift run --package-path experiments/apple-foundation-model apple-model-bridge --check
```

Run a small quick evaluation first. The default uses three nearest labelled examples per prompt plus the merchant lookup:

```bash
./.venv/bin/python src/evaluate_apple_model.py --limit 10
```

Run the complete held-out set with Apple's general model and three examples per prompt:

```bash
./.venv/bin/python src/evaluate_apple_model.py --model general --examples 3
```

Try retrieved examples, which supply the closest confirmed descriptions from the training portion only:

```bash
./.venv/bin/python src/evaluate_apple_model.py --model general --examples 3
```

Compare Apple's content-tagging variant separately:

```bash
./.venv/bin/python src/evaluate_apple_model.py --model content-tagging --examples 3
```

Run a small PCC comparison (each unresolved row is sent to Apple's service; PCC requires internet access and developer eligibility):

```bash
./.venv/bin/python src/evaluate_apple_model.py --model pcc --examples 3 --limit 20 --output data/apple-model-pcc-20.csv
```

The PCC bridge uses light reasoning. The short sample is a screening run only; compare against the printed local-classifier result on exactly the same rows before deciding whether a full evaluation is worthwhile. The framework's `isAvailable` check only reports device/service availability; it does not prove this executable has the required PCC entitlement.

The first invocation builds the Swift package. The full run can take several minutes because each unresolved transaction is classified individually. Evaluation output CSV contains transaction descriptions and is covered by `.gitignore`.

`--check` tests generation, not just the framework's availability flag. The on-device smoke check succeeded previously; a PCC smoke check requires the managed entitlement.

## PCC access status

The bridge builds with Xcode 27. Without Apple's managed PCC entitlement (`com.apple.developer.private-cloud-compute`), PCC requests fail with `LanguageModelError -1` wrapping `ModelManagerError 1046`. The bridge now checks for that entitlement before creating the PCC model. Apply for PCC access and run the evaluator from an executable signed with the granted entitlement before attempting PCC runs.

## Direct JSON Lines bridge

Each input line is an object with `id`, `description`, `transaction_type`, signed `amount_pence`, `categories`, and optional labelled `examples`. The bridge emits one JSON result per line. Positive amounts mean inflow; negative amounts mean outflow. It uses greedy generation and does not report a model-generated confidence score.

# Model card — Phase 6 custom image classifier

**Status: no evaluation has been run in this repository.**

`training/evaluate.py` overwrites this file with real metrics after you capture
a dataset, split it by session, train a checkpoint, and run the locked test set
once. Until then there is no accuracy, precision or recall number to report.

## What will be recorded here

- The base model and checkpoint path.
- Image counts per class and per split (train/val/test).
- Overall test accuracy and per-class precision/recall.
- A confusion-matrix image and the list of misclassified test files.
- The evaluation date and the limitation that the result applies only to the
  captured dataset.

## Limitations (even after a real run)

- Results apply only to the images in the manifest: this camera, lighting and
  part set. They are not a claim of accuracy on other parts or factories.
- The test split is session-disjoint, but it is a small local sample; the
  confidence intervals will be wide.
- Class balance, label noise and near-duplicate frames are not audited beyond
  the split-integrity check.
- This is not a production validation or a safety certification.

## Command

```bash
python -m training.evaluate \
  --checkpoint training/runs/classifier/best \
  --manifest data/manifest.csv \
  --data-dir data
```

from pathlib import Path
import json
from sklearn.ensemble import RandomForestClassifier

from cropharvest.datasets import CropHarvest
from cropharvest.utils import DATAFOLDER_PATH

from config import SHUFFLE_SEEDS, DATASET_TO_SIZES


MODEL_NAME = "RF"


def run(data_folder: Path = DATAFOLDER_PATH) -> None:
    evaluation_datasets = CropHarvest.create_benchmark_datasets(data_folder)
    results_folder = data_folder / MODEL_NAME
    results_folder.mkdir(exist_ok=True)

    for dataset in evaluation_datasets:

        sample_sizes = DATASET_TO_SIZES[dataset.id]

        for seed in SHUFFLE_SEEDS:
            dataset.shuffle(seed)
            for sample_size in sample_sizes:
                print(f"Running Random Forest for {dataset}, seed: {seed} with size {sample_size}")

                results_json = results_folder / f"{dataset.id}_{sample_size}_{seed}.json"
                results_nc = results_folder / f"{dataset.id}_{sample_size}_{seed}.nc"
                if results_json.exists():
                    print(f"Results already saved for {results_json} - skipping")

                train_x, train_y = dataset.as_array(flatten_x=True, num_samples=sample_size)

                # train a model
                model = RandomForestClassifier()
                model.fit(train_x, train_y)

                for _, test_instance in dataset.test_data(flatten_x=True):
                    preds = model.predict_proba(test_instance.x)[:, 1]

                    results = test_instance.evaluate_predictions(preds)

                    with Path(results_json).open("w") as f:
                        json.dump(results, f)

                    ds = test_instance.to_xarray(preds)
                    ds.to_netcdf(results_nc)


if __name__ == "__main__":
    run()
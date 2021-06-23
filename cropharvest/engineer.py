from pathlib import Path
from datetime import datetime, timedelta
import geopandas
from dataclasses import dataclass
import numpy as np
import pandas as pd
import xarray as xr
import rasterio
from rasterio import mask
from tqdm import tqdm
import warnings
import pickle

from cropharvest.eo import STATIC_BANDS, DYNAMIC_BANDS
from .config import (
    EXPORT_END_DAY,
    EXPORT_END_MONTH,
    LABELS_FILENAME,
    DAYS_PER_TIMESTEP,
    NUM_TIMESTEPS,
    TEST_REGIONS,
)
from .utils import DATAFOLDER_PATH

from typing import cast, Optional, Dict, Union, Tuple, List, Sequence

REMOVED_BANDS = ["B1", "B10"]
RAW_BANDS = DYNAMIC_BANDS + STATIC_BANDS
BANDS = [x for x in DYNAMIC_BANDS if x not in REMOVED_BANDS] + STATIC_BANDS + ["NDVI"]


@dataclass
class DataInstance:

    dataset: str
    label_lat: float
    label_lon: float
    instance_lat: float
    instance_lon: float
    array: np.ndarray
    is_crop: int
    label: Optional[str] = None


MISSING_DATA = -1


@dataclass
class TestInstance:
    x: np.ndarray
    y: np.ndarray  # 1 is positive, 0 is negative and -1 (MISSING_DATA) is no label
    lats: np.ndarray
    lons: np.ndarray


class Engineer:
    def __init__(self, data_folder: Path = DATAFOLDER_PATH) -> None:
        self.data_folder = data_folder
        self.eo_files = data_folder / "eo_data"
        self.test_eo_files = data_folder / "test_eo_data"
        self.labels = geopandas.read_file(data_folder / LABELS_FILENAME)
        self.labels["export_end_date"] = pd.to_datetime(self.labels.export_end_date)

        self.savedir = data_folder / "features"
        self.savedir.mkdir(exist_ok=True)

        self.test_savedir = data_folder / "test_features"
        self.test_savedir.mkdir(exist_ok=True)

        self.norm_interim: Dict[str, Union[np.ndarray, int]] = {"n": 0}

    @staticmethod
    def find_nearest(array, value: float) -> float:
        array = np.asarray(array)
        idx = (np.abs(array - value)).argmin()
        return array[idx]

    @staticmethod
    def process_filename(filename: str) -> Tuple[int, str]:
        r"""
        Given an exported sentinel file, process it to get the dataset
        it came from, and the index of that dataset
        """
        parts = filename.split("_")[0].split("-")
        index = parts[0]
        dataset = "-".join(parts[1:])
        return int(index), dataset

    @staticmethod
    def load_tif(filepath: Path, start_date: datetime) -> Tuple[xr.DataArray, float]:
        r"""
        The sentinel files exported from google earth have all the timesteps
        concatenated together. This function loads a tif files and splits the
        timesteps
        """

        da = xr.open_rasterio(filepath).rename("FEATURES")

        da_split_by_time: List[xr.DataArray] = []

        bands_per_timestep = len(DYNAMIC_BANDS)
        num_bands = len(da.band)
        num_dynamic_bands = num_bands - len(STATIC_BANDS)

        assert num_dynamic_bands == bands_per_timestep * NUM_TIMESTEPS

        static_data = da.isel(band=slice(num_bands - len(STATIC_BANDS), num_bands))
        average_slope = np.nanmean(static_data.values[STATIC_BANDS.index("slope"), :, :])

        for timestep in range(NUM_TIMESTEPS):
            time_specific_da = da.isel(
                band=slice(timestep * bands_per_timestep, (timestep + 1) * bands_per_timestep)
            )
            time_specific_da = xr.concat([time_specific_da, static_data], "band")
            time_specific_da["band"] = range(bands_per_timestep + len(STATIC_BANDS))
            da_split_by_time.append(time_specific_da)

        timesteps = [
            start_date + timedelta(days=DAYS_PER_TIMESTEP) * i for i in range(NUM_TIMESTEPS)
        ]

        dynamic_data = xr.concat(da_split_by_time, pd.Index(timesteps, name="time"))
        dynamic_data.attrs["band_descriptions"] = BANDS

        return dynamic_data, average_slope

    def update_normalizing_values(self, array: np.ndarray) -> None:
        # given an input array of shape [timesteps, bands]
        # update the normalizing dict
        # https://en.wikipedia.org/wiki/Algorithms_for_calculating_variance
        # https://www.johndcook.com/blog/standard_deviation/
        num_bands = array.shape[1]

        # initialize
        if "mean" not in self.norm_interim:
            self.norm_interim["mean"] = np.zeros(num_bands)
            self.norm_interim["M2"] = np.zeros(num_bands)

        for time_idx in range(array.shape[0]):
            self.norm_interim["n"] += 1

            x = array[time_idx, :]

            delta = x - self.norm_interim["mean"]
            self.norm_interim["mean"] += delta / self.norm_interim["n"]
            self.norm_interim["M2"] += delta * (x - self.norm_interim["mean"])

    def calculate_normalizing_dict(self) -> Optional[Dict[str, np.ndarray]]:

        if "mean" not in self.norm_interim:
            print("No normalizing dict calculated! Make sure to call update_normalizing_values")
            return None

        variance = self.norm_interim["M2"] / (self.norm_interim["n"] - 1)
        std = np.sqrt(variance)
        return {"mean": self.norm_interim["mean"], "std": std}

    @staticmethod
    def adjust_normalizing_dict(
        dicts: Sequence[Tuple[int, Optional[Dict[str, np.ndarray]]]]
    ) -> Optional[Dict[str, np.ndarray]]:

        for _, single_dict in dicts:
            if single_dict is None:
                return None

        dicts = cast(Sequence[Tuple[int, Dict[str, np.ndarray]]], dicts)

        new_total = sum([x[0] for x in dicts])

        new_mean = sum([single_dict["mean"] * length for length, single_dict in dicts]) / new_total

        new_variance = (
            sum(
                [
                    (single_dict["std"] ** 2 + (single_dict["mean"] - new_mean) ** 2) * length
                    for length, single_dict in dicts
                ]
            )
            / new_total
        )

        return {"mean": new_mean, "std": np.sqrt(new_variance)}

    @staticmethod
    def calculate_ndvi(input_array: np.ndarray) -> np.ndarray:
        r"""
        Given an input array of shape [timestep, bands] or [batches, timesteps, shapes]
        where bands == len(bands), returns an array of shape
        [timestep, bands + 1] where the extra band is NDVI,
        (b08 - b04) / (b08 + b04)
        """
        band_1, band_2 = "B8", "B4"

        num_dims = len(input_array.shape)
        if num_dims == 2:
            band_1_np = input_array[:, BANDS.index(band_1)]
            band_2_np = input_array[:, BANDS.index(band_2)]
        elif num_dims == 3:
            band_1_np = input_array[:, :, BANDS.index(band_1)]
            band_2_np = input_array[:, :, BANDS.index(band_2)]
        else:
            raise ValueError(f"Expected num_dims to be 2 or 3 - got {num_dims}")

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="invalid value encountered in true_divide")
            # suppress the following warning
            # RuntimeWarning: invalid value encountered in true_divide
            # for cases where near_infrared + red == 0
            # since this is handled in the where condition
            ndvi = np.where(
                (band_1_np + band_2_np) > 0, (band_1_np - band_2_np) / (band_1_np + band_2_np), 0
            )
        return np.append(input_array, np.expand_dims(ndvi, -1), axis=-1)

    @staticmethod
    def fillna(array: np.ndarray, average_slope: float) -> Optional[np.ndarray]:
        r"""
        Given an input array of shape [timesteps, BANDS]
        fill NaN values with the mean of each band across the timestep
        """
        num_dims = len(array.shape)
        if num_dims == 2:
            bands_index = 1
            mean_per_band = np.nanmean(array, axis=0)
        elif num_dims == 3:
            bands_index = 2
            mean_per_band = np.nanmean(np.nanmean(array, axis=0), axis=0)
        else:
            raise ValueError(f"Expected num_dims to be 2 or 3 - got {num_dims}")

        assert array.shape[bands_index] == len(BANDS)

        if np.isnan(mean_per_band).any():
            if (sum(np.isnan(mean_per_band)) == bands_index) & (
                np.isnan(mean_per_band[BANDS.index("slope")]).all()
            ):
                mean_per_band[BANDS.index("slope")] = average_slope
                assert not np.isnan(mean_per_band).any()
            else:
                return None
        for i in range(array.shape[bands_index]):
            if num_dims == 2:
                array[:, i] = np.nan_to_num(array[:, i], nan=mean_per_band[i])
            elif num_dims == 3:
                array[:, :, i] = np.nan_to_num(array[:, :, i], nan=mean_per_band[i])
        return array

    @staticmethod
    def remove_bands(array: np.ndarray) -> np.ndarray:
        """
        Expects the input to be of shape [timesteps, bands] or
        [batches, timesteps, bands]
        """
        num_dims = len(array.shape)
        if num_dims == 2:
            bands_index = 1
        elif num_dims == 3:
            bands_index = 2
        else:
            raise ValueError(f"Expected num_dims to be 2 or 3 - got {num_dims}")

        indices_to_remove: List[int] = []
        for band in REMOVED_BANDS:
            indices_to_remove.append(RAW_BANDS.index(band))
        indices_to_keep = [
            i for i in range(array.shape[bands_index]) if i not in indices_to_remove
        ]
        if num_dims == 2:
            return array[:, indices_to_keep]
        elif num_dims == 3:
            return array[:, :, indices_to_keep]

    def process_test_file(self, path_to_file: Path) -> Tuple[str, TestInstance]:
        id_components = path_to_file.name.split("_")
        crop, end_year = id_components[1], id_components[2]
        identifier = "_".join(id_components[:4])
        start_date = datetime(int(end_year), EXPORT_END_MONTH, EXPORT_END_DAY) - timedelta(
            days=NUM_TIMESTEPS * DAYS_PER_TIMESTEP
        )
        da, average_slope = self.load_tif(path_to_file, start_date=start_date)
        lon, lat = np.meshgrid(da.x.values, da.y.values)
        flat_lat, flat_lon = (
            np.squeeze(lat.reshape(-1, 1), -1),
            np.squeeze(lon.reshape(-1, 1), -1),
        )

        x_np = da.values
        x_np = x_np.reshape(x_np.shape[0], x_np.shape[1], x_np.shape[2] * x_np.shape[3])
        x_np = np.moveaxis(x_np, -1, 0)
        x_np = self.calculate_ndvi(x_np)
        x_np = self.remove_bands(x_np)
        x_np = self.fillna(x_np, average_slope)

        # finally, we need to calculate the mask
        region_bbox = TEST_REGIONS[identifier]
        relevant_indices = self.labels.apply(
            lambda x: (
                region_bbox.contains(x.lat, x.lon) and (x.export_end_date.year == int(end_year))
            ),
            axis=1,
        )
        relevant_rows = self.labels[relevant_indices]
        positive_geoms = relevant_rows[relevant_rows.label == crop].geometry.tolist()
        negative_geoms = relevant_rows[relevant_rows.label != crop].geometry.tolist()

        with rasterio.open(path_to_file) as src:
            # the mask is True outside shapes, and False inside shapes. We want the opposite
            positive, _, _ = mask.raster_geometry_mask(src, positive_geoms, crop=False)
            negative, _, _ = mask.raster_geometry_mask(src, negative_geoms, crop=False)
        # reverse the booleans so that 1 = in the
        positive = (~positive.reshape(positive.shape[0] * positive.shape[1])).astype(int)
        negative = (~negative.reshape(negative.shape[0] * negative.shape[1])).astype(int) * -1
        y = positive + negative

        # swap missing and negative values, since this will be easier to use in the future
        negative = y == -1
        missing = y == 0
        y[negative] = 0
        y[missing] = MISSING_DATA
        assert len(y) == x_np.shape[0]

        return identifier, TestInstance(x=x_np, y=y, lats=flat_lat, lons=flat_lon)

    def process_single_file(self, path_to_file: Path, row: pd.Series) -> Optional[DataInstance]:
        start_date = row.export_end_date - timedelta(days=NUM_TIMESTEPS * DAYS_PER_TIMESTEP)
        da, average_slope = self.load_tif(path_to_file, start_date=start_date)
        closest_lon = self.find_nearest(da.x, row.lon)
        closest_lat = self.find_nearest(da.y, row.lat)

        labelled_np = da.sel(x=closest_lon).sel(y=closest_lat).values

        labelled_np = self.calculate_ndvi(labelled_np)
        labelled_np = self.remove_bands(labelled_np)

        labelled_array = self.fillna(labelled_np, average_slope)
        if labelled_array is None:
            return None
        self.update_normalizing_values(labelled_array)

        return DataInstance(
            label_lat=row.lat,
            label_lon=row.lon,
            instance_lat=closest_lat,
            instance_lon=closest_lon,
            array=labelled_array,
            is_crop=row.is_crop,
            label=row.label,
            dataset=row.dataset,
        )

    def pickle_test_instances(
        self,
    ) -> None:
        test_savedir = self.savedir / "test_arrays"
        test_savedir.mkdir(exist_ok=True)

        for filepath in tqdm(list(self.test_eo_files.glob("*.tif"))):
            instance_name, test_instance = self.process_test_file(filepath)

            with (self.test_savedir / f"{instance_name}.pkl").open("wb") as f:
                pickle.dump(test_instance, f)

    def create_pickled_dataset(
        self,
        checkpoint: bool = True,
    ) -> None:
        arrays_dir = self.savedir / "arrays"
        arrays_dir.mkdir(exist_ok=True)

        old_normalizing_dict: Optional[Tuple[int, Optional[Dict[str, np.ndarray]]]] = None
        if checkpoint:
            # check for an already existing normalizing dict
            if (self.savedir / "normalizing_dict.pkl").exists():
                with (self.savedir / "normalizing_dict.pkl").open("rb") as f:
                    old_nd = pickle.load(f)
                num_existing_files = len(list(arrays_dir.glob("*")))
                old_normalizing_dict = (num_existing_files, old_nd)

        skipped_files: int = 0
        num_new_files: int = 0
        for file_path in tqdm(list(self.eo_files.glob("*.tif"))):
            file_index, dataset = self.process_filename(file_path.name)
            file_name = f"{file_index}_{dataset}.pkl"
            if (checkpoint) & ((arrays_dir / file_name).exists()):
                # we check if the file has already been written
                continue

            file_row = self.labels[
                ((self.labels.dataset == dataset) & (self.labels["index"] == file_index))
            ].iloc[0]

            instance = self.process_single_file(
                file_path,
                row=file_row,
            )
            if instance is not None:
                with (arrays_dir / file_name).open("wb") as f:
                    pickle.dump(instance, f)
                num_new_files += 1
            else:
                skipped_files += 1

        print(f"Wrote {num_new_files} files, skipped {skipped_files} files")

        normalizing_dict = self.calculate_normalizing_dict()

        if checkpoint and (old_normalizing_dict is not None):
            normalizing_dicts = [old_normalizing_dict, (num_new_files, normalizing_dict)]
            normalizing_dict = self.adjust_normalizing_dict(normalizing_dicts)
        if normalizing_dict is not None:
            save_path = self.savedir / "normalizing_dict.pkl"
            with save_path.open("wb") as f:
                pickle.dump(normalizing_dict, f)
        else:
            print("No normalizing dict calculated!")
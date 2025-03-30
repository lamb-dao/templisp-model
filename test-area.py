#start emacs from model env in shell
# M-x spacemacs/force-init-spacemacs-env
# , n a activate model env
# , ' open inferior repl

################
# Contents
################
# * Setup
# ** Import
# ** Path and file selection
# ** Global variables
# ** Input validation
# * AOI
# ** Define
# ** Create Grid
# * Load EOpatch
# ** Load rasters
# ** Load timestamps etc
# ** Load reference polygons
# *** Bind identities and observations
# *** Import vectors
# *** Rasterize observations
# ** Visualize layers
# *** Object contents
# *** RGB per time
# *** Reference identities map
# *** Rasterized observations
# * Prepare eopatch
# ** Concatenate
# ** Erosion
# ** Sampling
# * Create training data
# ** Extract eopatch
# ** Reshape data
# *** Split patches into train test for GBM
# *** Shape for GBM
# *** Split samples into train test for TST &&&
# *** Shape for TSP
# * GBM experiment
# ** Train
# ** Validate
# *** F1 etc table
# *** Confusion matrices
# *** Class balance
# *** ROC and AUC
# *** Feature importance
# ** Predict
# *** visualize prediction
# ** Quantify prediction
# *** Visualize predicted trait
# *** Visualize trait diff
# *** Quantify agreement
# * TST experiment
# ** Train
# *** Unsupervised training
# *** Transfer learning
# ** Validate
# *** F1 etc table
# *** Confusion matrices
# *** Class balance
# *** ROC and AUC
# *** Feature importance
# ** Predict
# *** visualize prediction
# ** Quantify prediction
# *** Visualize predicted trait
# *** Visualize trait diff
# *** Quantify agreement



################
# * Setup
################

######### ** Import
import os
import pathlib
import glob
import re

import shapely.geometry
import shapely.validation
from copy import deepcopy
from osgeo import gdal
from fiona.collection import BytesCollection
from collections import defaultdict
import sys
import itertools
import joblib
import pandas as pd
from pandas._config import dates
from tqdm.auto import tqdm
import datetime
import numpy as np
from sklearn import metrics, preprocessing
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap
from aenum import MultiValueEnum
from shapely.geometry import Polygon
import lightgbm as lgb
import rasterio
import geopandas as gpd
from sentinelhub import DataCollection, UtmZoneSplitter

from eolearn.core.core_tasks import CreateEOPatchTask, InitializeFeatureTask, RemoveFeatureTask
from eolearn.core import (EOExecutor,
                          EOPatch,
                          EOTask,
                          EOWorkflow,
                          FeatureType,
                          LoadTask,
                          MergeFeatureTask,
                          OverwritePermission,
                          SaveTask,
                          linearly_connect_tasks,)
from eolearn.io import ImportFromTiffTask, ExportToTiffTask, VectorImportTask
from eolearn.core.eonode import EONode
from eolearn.features import NormalizedDifferenceIndexTask, SimpleFilterTask
from eolearn.features.extra.interpolation import LinearInterpolationTask
from eolearn.geometry import ErosionTask, VectorToRasterTask
from eolearn.ml_tools import FractionSamplingTask

######### ** Path and file selection
# input data
DATA_ROOT= pathlib.Path("/bulk-2/2023-package")
# set expected dirs
DATA_AREAS = os.path.join(DATA_ROOT, "area_poly")
DATA_IDS = os.path.join(DATA_ROOT, "id_poly")
DATA_RASTERS = os.path.join(DATA_ROOT, "test-rasters") #dir rasters or test-rasters
DATA_TABLE= os.path.join(DATA_ROOT, "tabular")
# set expected files
DATA_train = os.path.join(DATA_AREAS, "test-AOI-north.gpkg")
DATA_test = os.path.join(DATA_AREAS, "test-AOI-south.gpkg")
DATA_all =  os.path.join(DATA_AREAS, "test-AOI.gpkg")
DATA_ids = os.path.join(DATA_IDS, "identities.gpkg")
DATA_table = os.path.join(DATA_TABLE, "field-data.csv")

# intermediate and output data
DATA_OP_ROOT = os.path.join(DATA_ROOT, "..", "model_output")
EOPATCH_TRAIN_DIR= os.path.join(DATA_OP_ROOT, "eopatches_train")
EOPATCH_VALIDATE_DIR= os.path.join(DATA_OP_ROOT, "eopatches_validation")
DATA_DIR= os.path.join(DATA_OP_ROOT, "data")
EOPATCH_SAMPLES_DIR= os.path.join(DATA_OP_ROOT, "eopatches_sampled")
RESULTS_DIR= os.path.join(DATA_OP_ROOT, "results")

######### ** Global variables
RNDM = 42
np.random.seed(RNDM)
CRS = "EPSG:32614"
GRID_SIZE = 500 # pixel count of patch edge, reduce to relieve RAM
RESOLUTION = 0.003 # gsd in meters
EXPECTED_N_TIFS = 630
EXPECTED_INDICES = ['nir', 'red_edge', 'red', 'green', 'blue', 'ndvi', 'sentera_ndre'] # check for expected unique indices, order irrelevant
USED_INDICES = ['nir', 'red_edge', 'red', 'green', 'blue'] # set order and spectra to use, must be subset of expected

######### ** Input validation
def dir_file_enforce():
    # check exists
    for d in (DATA_ROOT, DATA_AREAS, DATA_IDS, DATA_RASTERS, DATA_TABLE):
        if not os.path.exists(d):
            raise FileNotFoundError(f"Input directory not found: {d}")
    for f in (DATA_train, DATA_test, DATA_all, DATA_ids, DATA_table):
        if not os.path.exists(f):
            raise FileNotFoundError(f"Input file not found: {f}")
    # make exist
    for d in (DATA_OP_ROOT, EOPATCH_TRAIN_DIR, EOPATCH_VALIDATE_DIR, DATA_DIR, EOPATCH_SAMPLES_DIR, RESULTS_DIR):
        os.makedirs(d, exist_ok=True)

def parse_identifiers(path):
    "extract date index and sigma from a path"
    date_pat = re.compile(r'date_(.+?)_index')
    index_pat = re.compile(r'index_(.+?)_sigma')
    sigma_pat = re.compile(r'sigma-(.+?)\.tif')

    fileName = os.path.basename(path)

    date_found = str()
    index_found = str()
    sigma_found = str()

    date_match = date_pat.search(fileName)
    if date_match:
        date_found = (date_match.group(1))
    else:
        print(f"WARNING: No date match found in the path: {path}")

    index_match = index_pat.search(fileName)
    if index_match:
        index_found  = (index_match.group(1))
    else:
        print(f"WARNING: No index match found in the path: {path}")

    sigma_match = sigma_pat.search(fileName)
    if sigma_match:
        sigma_found = (sigma_match.group(1))
    else:
        print(f"WARNING: No sigma match found in the path: {path}")

    return {'date':date_found,
            'index':index_found,
            'sigma':sigma_found}

def input_tifs():
    tifs = glob.glob(os.path.join(DATA_RASTERS, "*.tif"))
    return tifs

def validate_input_files(tifs=input_tifs(), expected_n_tifs=EXPECTED_N_TIFS, expected_indices=EXPECTED_INDICES, used_indices=USED_INDICES):
    "Validate expected qualities of input tifs."

    # check n of tifs as expected
    if not len(tifs) ==  expected_n_tifs:
        raise Error(f"The number of tifs is not the expected {expected_n_tifs}. Found: {len(tifs)}")

    # check indices are as expected
    indices = set()
    for tif in tifs:
        index = parse_identifiers(tif)['index']
        indices.add(index)
        # print nodata value if weird
        with rasterio.open(tif) as src:
            nd_value = src.nodata
            if nd_value != -10000:
                print(f"gtiff: {tif}")
                print(f"Nodata value: {nd_value}")
    if not indices ==  set(expected_indices):
        raise ValueError(f"The indices are not those expected. Found: {indices}")
    # check globally set using is subset of found
    if not(set(used_indices) <= indices):
        raise ValueError("Expected found unique indices to be a superset of used_indices")

    # all share projection, shape and extent
    ref_ds = gdal.Open(tifs[0])
    ref_proj = ref_ds.GetProjection()
    ref_geotransform = ref_ds.GetGeoTransform()
    ref_shape =(ref_ds.RasterYSize, ref_ds.RasterXSize)
    for tif in tifs[1:]:
        ds = gdal.Open(tif)
        if ds.GetProjection() != ref_proj:
            print(ds.GetProjection())
            raise ValueError(f"Projection mismatch in {tif}")
        if ds.GetGeoTransform() != ref_geotransform:
            print("WARNING, potential geotransform mismatch")
            print(f"expected: {ref_geotransform}")
            print(f"found   : {ds.GetGeoTransform()}")
            print(os.path.basename(tif))
        if (ds.RasterYSize, ds.RasterXSize) != ref_shape:
            raise ValueError (f"Shape mismatch in {tif}")

    # crs checks
    for f in [DATA_train, DATA_test, DATA_all, DATA_ids]:
        extent = gpd.read_file(f)
        crs = extent.crs
        if not crs == CRS:
            raise ValueError(f"Unexpected crs for file: {f} crs: {crs}")

dir_file_enforce()
validate_input_files()
# for test are and for full area
# &&& 2023-09-05 potential geotransform mismatch
# &&& 2023-07-24 Nodata value is None


################
# * AOI
################
######### ** Define

def area_grid(area, grid_size=GRID_SIZE, resolution=RESOLUTION, show=False):
    """
   read in gpkg file compute patches by pixels

    Args:
        area: the AOI geopackage with area shape
        grid_size: patch size to split the AOI into
        resolution: gsd of data in meters
    Return:
        bbox-list: an array of bounding boxes
    """

    # Load geopackage file
    extent = gpd.read_file(area)
    # Get the shape in polygon format
    extent_shape = extent.geometry.values[0]
    # get the width and height of the area in meters
    width = extent_shape.bounds[2] - extent_shape.bounds[0]
    height = extent_shape.bounds[3] - extent_shape.bounds[1]
    print(f"Dimension of the area is: {width:.0f} x {height:.0f} m")
    # get the width and height of the area in pixels
    width_pix = int(width / resolution)
    height_pix = int(height / resolution)
    print(f"Dimension of the area is {width_pix} x {height_pix} pixels")
    # &&& get the number of grid cells wide and tall our area is
    # get the width and height of the area in patches
    width_patch = int(round(width_pix / grid_size))
    height_patch =  int(round(height_pix / grid_size))
    print(f"Dimension of the area is {width_patch} x {height_patch} patches (rounded to nearest full patch)")
    # length of patch edge m=(m/px)*px
    edge_m =resolution*grid_size

    # if show:
    #     plt.ion()
    #     extent.plot()
    #     plt.axis("off")
    #     plt.close()

    # Create a splitter to obtain a list of bboxes
    bbox_splitter = UtmZoneSplitter([extent_shape], extent.crs, edge_m)
    bbox_list = np.array(bbox_splitter.get_bbox_list())
    info_list = np.array(bbox_splitter.get_info_list())

    # Prepare info of selected EOPatches
    geometry = [Polygon(bbox.get_polygon()) for bbox in bbox_list]
    idxs = [info["index"] for info in info_list]
    idxs_x = [info["index_x"] for info in info_list]
    idxs_y = [info["index_y"] for info in info_list]
    bbox_gdf = gpd.GeoDataFrame({"index": idxs, "index_x": idxs_x, "index_y": idxs_y}, crs=extent.crs, geometry=geometry)

    #collect patches
    patch_ids = []
    for idx, info in enumerate(info_list):
        patch_ids.append(idx)

    if show:
        # Plot the polygon and its partitions

        plt.ion()
        fig, ax = plt.subplots(figsize=(30, 30))
        ax.set_title(f"Area: {area}, partitioned to patches", fontsize=25)
        extent.plot(ax=ax, facecolor="w", edgecolor="b", alpha=0.5) # area shape
        bbox_gdf.plot(ax=ax, facecolor="w", edgecolor="r", alpha=0.5) # patch grid
        for bbox, info in zip(bbox_list, info_list):
            geo = bbox.geometry
            ax.text(geo.centroid.x, geo.centroid.y, info["index"], ha="center", va="center")
        plt.axis("off");

    print(f"Total patches is: {len(bbox_list)}")
    return bbox_list

######### ** Create Grid
area_grid(DATA_train, show=True)

################
# * Load EOpatch
################

######### ** Load rasters

# tif file set handling tools
def unique_tif_indicators(tifs=input_tifs(), used_indices=USED_INDICES):
    " Return correctly ordered lists of: unique dates, globally selected used_indices, and unique sigma values "

    rawdates = set()
    rawindices = set()
    rawsigmas = set()

    for file in tifs:
        parsed = parse_identifiers(file)
        # no expected match may be empty
        if parsed['date'] == '' or parsed['index'] == '':
            raise RuntimeError(f"A file has no unique match: {file}")
        rawdates.add(parsed['date'])
        rawindices.add(parsed['index'])
        rawsigmas.add(parsed['sigma'])

    # rawindices must be a superset of USED_INDICES
    if not set(used_indices) <= rawindices:
        raise AssertionError(f"The raw indices are not a super set of USED_INDICES. raw: {rawindices}")

    dates = sorted(list(rawdates)) # alphanumeric order is time order
    indices = used_indices  # use to impose sort by freq, known correct due to validation and subset test
    sigmas = sorted(list(map(float, rawsigmas))) # sorted list of numbers

    return {'dates':dates,
            'indices':indices,
            'sigmas':sigmas}

def select_tif_path(select_date, select_index, select_sigma, tifs=input_tifs() ):
    "Using intersect of dates indices and sigmas, return a single matching path"
    a = set() # dates
    b = set() # indices
    c = set() # sigmas

    for file in tifs:
        parsed = parse_identifiers(file)
        if parsed['date'] == select_date:
            a.add(file)
        if parsed['index'] == select_index:
            b.add(file)
        if float(parsed['sigma']) == float(select_sigma):
            c.add(file)

    selected = a & b & c
    if len(selected) > 1:
        raise ValueError(f"Selected is longer than one. Selected: {selected}")
    if len(selected) == 0:
        raise ValueError(f"No match found")
    return list(selected)[0]
# select_tif_path(unique_tif_indicators()['dates'][1], unique_tif_indicators()['indices'][1], unique_tif_indicators()['sigmas'][1])

def select_tif_set(date_list, index_list, sigma_list):
    "sets of tif files ordered as imposed in lists returned from unique_tif_indicators"
    selects = list()
    for i in date_list:
        for j in index_list:
            for k in sigma_list:
                sel = select_tif_path(i, j, k)
                selects.append(sel)
    return selects
# select_tif_set(unique_tif_indicators()['dates'][1:2], unique_tif_indicators()['indices'][1:2], unique_tif_indicators()['sigmas'][1:2])
# len( select_tif_set(unique_tif_indicators()['dates'],['red'],[2.0]))

# define tasks for load workflow
def make_workflow_name(n, i, s):
    "assemble name for the date-set of tiffs, like 0000_index_<>_sigma_<>-<> "
    # pad the counter to maintain ordering
    zeroPaddedN = f"{n:0{4}d}"
    unsafe = f"{zeroPaddedN}_index_{i}_sigma_{s}"
    # need to drop decimals,invalid in feature names
    safe = unsafe.replace('.', '-')
    return safe

def taskList_to_nodeList(taskList):
    "connects a list of tasks as EONodes, returning a list of nodes"
    nodes = []
    node_previous = None

    for task in taskList:
        node_current = None
        if node_previous is None:
            # the first node has no previous dependency
            node_current = EONode(task)
        else:
            # all other nodes depend on the previous
            node_current = EONode(task, inputs=[node_previous])
        # add the Node and assign previous
        nodes.append(node_current)
        node_previous = node_current
    return nodes

def stack_timeless_features(*features):
    "Merge multiple DATA_TIMELESS features into a time-series DATA feature"
    print(f"Merging {len(features)} features")

    for i, feat in enumerate(features):
        print(f"feature: {i}")
        print(f"  Shape: {feat.shape}")
        print(f"  Dtype: {feat.dtype}")

    stacked= np.stack(features, axis=0)
    print(f"Stacked shape: {stacked.shape}")
    return stacked

def CreateStackLoaderWorkflow(indicators, areas, eopatch_dir):
    """
    Creates a workflow to import tiffs individually, concatenate sets on the time axis, and delete the individuals.
    Args:
    indicators=unique_tif_indicators() order of dates,indices,sigmas determines final order in eopatch
    areas=area_grid(DATA_train) or area_grid(DATA_test) grid determines parallell processing and patch size
    Rets:
    workflow
    execution_args for workflow
    """

    dates = indicators['dates']
    indices = indicators['indices']
    sigmas = indicators['sigmas']

    n = 0 # counter padded and prepended to name to force ordering of sets
    tasklist = [] # aggregate the set of tasks
    create_task = CreateEOPatchTask() # initialize, will accept bbox for the entire chain
    tasklist.append(create_task)
    #SET actions, all dates are used
    for i in indices:
        for s in sigmas:
            name_set = make_workflow_name(n, i, s)
            tiffs = select_tif_set(dates, [i], [s])
            #SINGLE actions
            m = 0 # identifiers for singles of a set, pad and prepend
            single_list = []
            for tif in tiffs:
                name_single = f"{m:0{3}d}_{name_set}"
                single_data_feature = (FeatureType.DATA_TIMELESS, name_single)
                load_task = ImportFromTiffTask(feature=single_data_feature, path=tif)
                tasklist.append(load_task)
                single_list.append(single_data_feature)
                m = m + 1
            # concatenate all single layers to a set
            set_data_feature = (FeatureType.DATA, name_set)
            # zip_function:  expand dims to 4d (1t*h*w*1c), then concat along time to 3d (t*h*w*1c)
            merge_task = MergeFeatureTask(single_list, set_data_feature, zip_function=stack_timeless_features)
            tasklist.append(merge_task)
            # delete task
            remove_task = RemoveFeatureTask(single_list)
            tasklist.append(remove_task)
            n = n + 1
    save_task = SaveTask(eopatch_dir, overwrite_permission=OverwritePermission.OVERWRITE_PATCH)
    tasklist.append(save_task)

    workflow_nodes = taskList_to_nodeList(tasklist)
    workflow = EOWorkflow(workflow_nodes)
    # additional arguments
    execution_args = []
    for idx, bbox in enumerate(areas):
        args_dict = {workflow_nodes[0]: {"bbox": bbox}, # create task is first
                     workflow_nodes[-1]: {"eopatch_folder": f"eopatch_{idx}"} # save task is last
                     }
        execution_args.append(args_dict)
    return workflow, execution_args

def execute_prepared_workflow(workflow_and_args):
    # create workflow
    wf, args = workflow_and_args
    # Execute the workflow
    executor = EOExecutor(wf, args, save_logs=True)
    executor.run(workers=1) # workers must be 1 to avoid pickling error
    executor.make_report()

    failed_ids = executor.get_failed_executions()
    if failed_ids:
        raise RuntimeError(
            f"Execution failed EOPatches with IDs:\n{failed_ids}\n"
            f"For more info check report at {executor.get_report_path()}"
        )

# TIME INTENSIVE JOB load all geotiffs into training eopatches stacked
# execute_prepared_workflow(CreateStackLoaderWorkflow(indicators=unique_tif_indicators(), areas=area_grid(DATA_train), eopatch_dir=EOPATCH_TRAIN_DIR ))

######### ** Load timestamps etc

class AddTimestamps(EOTask):
    "Adds timestamps to an eopatch. uses the function unique_tif_indicators to identify times"
    def __init__(self, unique_tif_indicators=unique_tif_indicators()):
        self.indicators = unique_tif_indicators
        self.dates = self.indicators['dates']
    def _toDatetime(self, stringlist):
        fmt = '%Y-%m-%d'
        return [datetime.datetime.strptime(s, fmt) for s in stringlist]
    def execute(self, eopatch):
        eopatch.timestamps = self._toDatetime(self.dates)
        return eopatch

class MakeAreaMask(EOTask):
    "Uses polygon to make a valid area mask. Arg: path to geopackage containing geometry"
    def __init__(self, gpkg_path):
        self.gpkg_path = gpkg_path
    def _load_polygon(self):
        gdf = gpd.read_file(self.gpkg_path)
        return gdf.geometry.union_all()
    def _get_dimensions(self, eopatch):
        for feature_type, feature_name in eopatch.get_features():
            if feature_type == FeatureType.DATA:
                height, width = eopatch.get_spatial_dimension(feature_type, feature_name)
                break
        else:
            raise ValueError("No DATA feature found to infer spatial dimensions")
        return height, width
    def _get_transform(self, eopatch, height, width):
        bbox = eopatch.bbox
        tnsf = rasterio.transform.from_bounds(
            west=bbox.min_x,
            south=bbox.min_y,
            east=bbox.max_x,
            north=bbox.max_y,
            width=width,
            height=height
        )
        return tnsf
    def _get_mask(self, polygon, transform, height, width):
        mask = rasterio.features.geometry_mask(
            [polygon],
            out_shape =(height, width),
            transform = transform,
            invert = True # Pixel is inside geometry
        )
        return mask
    def execute(self, eopatch):
        height, width = self._get_dimensions(eopatch)
        transform = self._get_transform(eopatch, height, width)
        polygon = self._load_polygon()
        mask = self._get_mask(polygon, transform, height, width) # shape (n*m)
        mask3d = mask[..., np.newaxis] # add d for (n*m*d)
        # if FeatureType.MASK_TIMELESS not in eopatch:
        #     eopatch[FeatureType.MASK_TIMELESS] = {}
        eopatch[FeatureType.MASK_TIMELESS, "IN_POLYGON"] = mask3d.astype(bool)
        print(eopatch)
        return eopatch

######### ** Load reference polygons
#####
# *** Bind identities and observations

def report_repair_invalid_geometry(invalid_index, gdf):
    invalid_geom = gdf.loc[invalid_index, 'geometry']
    print("Invalid geometry",invalid_geom)
    explanation = shapely.validation.explain_validity(invalid_geom)
    print("Invalid explanation", explanation)
    repaired_geom = shapely.validation.make_valid(invalid_geom)
    print("Repaired geometry", repaired_geom)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 6))
    ax1.set_title("Original Geometry")
    ax2.set_title("Repaired Geometry")
    if hasattr(invalid_geom, 'exterior'):
        ax1.plot(*invalid_geom.exterior.xy)
    else:
        ax1.plot(invalid_geom.xy)
    if isinstance(repaired_geom, shapely.geometry.GeometryCollection):
        for geom in repaired_geom.geoms:
            if hasattr(geom, 'exterior'):
                ax2.plot(*geom.exterior.xy)
            else:
                ax2.plot(*geom.xy)
    elif hasattr(repaired_geom, 'exterior'):
        ax2.plot(*repaired_geom.exterior.xy)
    else:
        ax2.plot(*repaired_geom.xy)
    ax1.axis('equal')
    ax2.axis('equal')
    plt.show()

    return repaired_geom

def check_valid_geometry(gdf):
    gdf['is_valid'] = gdf.geometry.is_valid
    invalid_geoms = gdf[~gdf['is_valid']]

    print(f"Total geometries: {len(gdf)}")
    print(f"Valid geometries: {gdf['is_valid'].sum}")
    print(f"Invalid geometries: {len(invalid_geoms)}")
    print(f"Invalid indices: {invalid_geoms.index.tolist()}")

    repaired_gdf = deepcopy(gdf)
    for idx in invalid_geoms.index:
        repaired_gdf.loc[idx, 'geometry'] = report_repair_invalid_geometry(idx, gdf)

    return repaired_gdf

def bind_observations(polygons=DATA_ids, observations=DATA_table, ddir=DATA_DIR):
    "Append table of observation data to polygons, ensure common column, then row bind on samples. Returns: fiona readable object"
    gdf = gpd.read_file(polygons)
    gdf = check_valid_geometry(gdf)
    df = pd.read_csv(observations)
    # ensure correct attributes
    target_shared = 'sample'
    change_from = 'SAMPLE'
    cols_gdf = gdf.columns
    # must have target_shared
    if not target_shared in cols_gdf:
        raise ValueError(f"Expected column not found in {cols_gdf} Expected: {target_shared}")
    cols_df = df.columns
    # must have change_from
    if not change_from in cols_df:
        raise ValueError(f"Expected column not found in {cols_df} Expected: {change_from}")
    # make rename for merge
    df.rename(columns={change_from: target_shared}, inplace=True)
    # merge
    merged_gdf = gdf.merge(df, on=target_shared, how='left')
    # merged_gdf.columns
    # write to disk
    path = os.path.join(ddir, 'bound_observations.gpkg')
    abs_path = os.path.abspath(path)
    merged_gdf.to_file(abs_path, driver='GPKG', layer='name')
    return abs_path

def CreateDetailsLoaderWorkflow(areas, observations, eopatch_dir):
    """
    Creates a workflow to add dates, masks, rasterized observation data to eopatches.
    Args:
    areas=area_grid(DATA_train) or area_grid(DATA_test), grid determines patch count and size
    observations=bind_observations(), a path to a fiona readable object with identity geometries and observation data per identity
    eopatch_dir path to the loaded tif stack eopatches which this workflow will augment
    Rets:
    workflow
    execution_args for workflow
    """

    #####
    # *** Import vectors
    vector_feature = FeatureType.VECTOR_TIMELESS, "IDENTITIES"
    vector_import_task = VectorImportTask(vector_feature, observations)

    #####
    # *** Rasterize observations
    '''
    merged_gdf.columns for selection by values_column

    Index(['sample', 'geometry', 'NAME', 'HEIGHT-CM', 'ROW-TYPE',
           'HULLESS-CONDITION', 'SBLOTCH-RATING', 'WEIGHT', 'DIAMETER', 'AREA',
           'STEM-WEIGHT', 'DENSITY', 'ROWS', 'BARLEY-WHEAT', 'HULLED',
           'SBLOTCH-LMH'],
          dtype='object')

    which are valid for test area &&&
    '''

    rasterize_height_task = VectorToRasterTask(
        vector_feature, # as used in vector import task
        (FeatureType.DATA_TIMELESS, "HEIGHT"), #name of rasterized new layer, DATA floats
        values_column="HEIGHT-CM", # col of merged_gdf.columns to rasterize
        raster_shape=(FeatureType.MASK_TIMELESS, "IN_POLYGON"),
        raster_dtype=np.float32, # &&& float
    )

    # initialize tasks or copy
    load_task = LoadTask(eopatch_dir)
    clean_task = RemoveFeatureTask(features=[(FeatureType.DATA_TIMELESS, 'HEIGHT')])
    add_timestamps_task = AddTimestamps()
    make_areamask_task = MakeAreaMask(DATA_train)
    vector_task = vector_import_task
    rasterize_task = rasterize_height_task
    save_task = SaveTask(eopatch_dir, overwrite_permission=OverwritePermission.OVERWRITE_FEATURES)
    # node list
    #workflow_nodes = linearly_connect_tasks(load_task, add_timestamps_task, make_areamask_task, vector_task, rasterize_task, save_task)

    workflow_nodes = linearly_connect_tasks(load_task, vector_task, save_task)

    # workflow
    workflow = EOWorkflow(workflow_nodes)
    # additional arguments
    execution_args = []
    for idx, bbox in enumerate(areas):
        execution_args.append(
            {
                workflow_nodes[0]: {"eopatch_folder": f"eopatch_{idx}"}, # load task is first
                workflow_nodes[-1]: {"eopatch_folder": f"eopatch_{idx}"} # save task is last
            }
        )

    return workflow, execution_args

# wf,args =  CreateDetailsLoaderWorkflow(areas=area_grid(DATA_train), observations=bind_observations(), eopatch_dir=EOPATCH_TRAIN_DIR)

# load masks and timestamps to the patches after the gtiff stacks
# execute_prepared_workflow(CreateDetailsLoaderWorkflow(areas=area_grid(DATA_train), observations=bind_observations(), eopatch_dir=EOPATCH_TRAIN_DIR))




#&&& simplified version for testing error

def bind_observations_temp(polygons=DATA_ids, observations=DATA_table, ddir=DATA_DIR):
    "Minimal case, only geometries and single int attribute"
    gdf = gpd.read_file(polygons)
    validated_gdf = check_valid_geometry(gdf) #&&& add to official
    # write to disk
    path = os.path.join(ddir, 'temp_observations.gpkg')
    abs_path = os.path.abspath(path)
    validated_gdf.to_file(abs_path, driver='GPKG', layer='identities') #&&&
    return abs_path

# bind_observations_temp()
# '/bulk-2/model_output/data/temp_observations.gpkg'
# test = gpd.read_file('/bulk-2/model_output/data/temp_observations.gpkg')
# test.crs
# test.columns

vector_feature = (FeatureType.VECTOR_TIMELESS, "IDENTITIES")

# init tasks
load_task = LoadTask(EOPATCH_TRAIN_DIR)
vector_task = VectorImportTask(vector_feature, bind_observations_temp())
rasterize_height_task = VectorToRasterTask(
    vector_feature, # as used in vector import task
    (FeatureType.DATA_TIMELESS, "HEIGHT"), #name of rasterized new layer, DATA for float values
    values_column="HEIGHT-CM", # select col from merged_gdf.columns to rasterize
    raster_shape=(FeatureType.MASK_TIMELESS, "IN_POLYGON"),
    raster_dtype=np.float32, # float
)
# this target dir is made empty before each test
save_task = SaveTask('/home/user/temp', overwrite_permission=OverwritePermission.OVERWRITE_FEATURES)

# node list
# success
# workflow_nodes = linearly_connect_tasks(load_task, <other data loading tasks have been successfully saved>, save_task)
# success
# workflow_nodes = linearly_connect_tasks(load_task, vector_task)
# fail
workflow_nodes = linearly_connect_tasks(load_task, vector_task, save_task)
'''
test 2
manually place a copy of observations, as if vector_task had succeded to test rasterize height task

 cd /bulk-2/model_output/eopatch_0/vector_timeless/
 cp /bulk-2/model_output/data/IDENTITIES.gpkg .
 cd ../../eopatch_1/vector_timeless/
 cp /bulk-2/model_output/data/IDENTITIES.gpkg .
 cd ../../eopatch_2/vector_timeless/
 cp /bulk-2/model_output/data/IDENTITIES.gpkg .
 cd ../../eopatch_3/vector_timeless/
 cp /bulk-2/model_output/data/IDENTITIES.gpkg .

'''
# success
# workflow_nodes = linearly_connect_tasks(load_task, rasterize_height_task)
# fail
# workflow_nodes = linearly_connect_tasks(load_task, rasterize_height_task, save_task)

# workflow
workflow = EOWorkflow(workflow_nodes)

# additional arguments
execution_args = []
for idx, bbox in enumerate(area_grid(DATA_train)): # area_grid partitions the polygon and returns bbox list
    execution_args.append(
        {
            workflow_nodes[0]: {"eopatch_folder": f"eopatch_{idx}"}, # load task is first
            workflow_nodes[-1]: {"eopatch_folder": f"eopatch_{idx}"} # save task is last
        }
    )

# Execute the workflow
executor = EOExecutor(workflow, execution_args, save_logs=True)
executor.run(workers=1)
executor.make_report()

failed_ids = executor.get_failed_executions()
if failed_ids:
    raise RuntimeError(
        f"Execution failed EOPatches with IDs:\n{failed_ids}\n"
        f"For more info check report at {executor.get_report_path()}"
    )

'''

In both cases, the combination of a (vector import task or rasterize task) followed by the save task causes this pyogiro-sqlite error


>> error
2025-03-29 16:09:02,118 eolearn.core.eoworkflow ERROR    Task 'SaveTask' with id SaveTask-06f7db1f0ce211f09159-6bd05c84396c failed with stack trace:
Traceback (most recent call last):
  File "pyogrio/_io.pyx", line 1603, in pyogrio._io.ogr_create
  File "pyogrio/_err.pyx", line 179, in pyogrio._err.exc_wrap_pointer
pyogrio._err.CPLE_OpenFailedError: sqlite3_open(<gzip _io.BufferedWriter name=b'/home/user/temp/tmpcowhph8o__tempfs__/tmp_feature' 0x7213b77aaa10>) failed: unable to open database file
pyogrio OpenFailedError: sqlite3_open failed: unable to open database file

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/home/user/.conda/envs/model/lib/python3.11/site-packages/eolearn/core/eoworkflow.py", line 271, in _execute_task
    return task.execute(*task_args, **task_kwargs)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/user/.conda/envs/model/lib/python3.11/site-packages/eolearn/core/core_tasks.py", line 162, in execute
    eopatch.save(
  File "/home/user/.conda/envs/model/lib/python3.11/site-packages/eolearn/core/eodata.py", line 644, in save
    save_eopatch(
  File "/home/user/.conda/envs/model/lib/python3.11/site-packages/eolearn/core/eodata_io.py", line 162, in save_eopatch
    new_files = list(executor.map(lambda saver: saver(), data_for_saving))
                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/user/.conda/envs/model/lib/python3.11/concurrent/futures/_base.py", line 619, in result_iterator
    yield _result_or_cancel(fs.pop())
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/user/.conda/envs/model/lib/python3.11/concurrent/futures/_base.py", line 317, in _result_or_cancel
    return fut.result(timeout)
           ^^^^^^^^^^^^^^^^^^^
  File "/home/user/.conda/envs/model/lib/python3.11/concurrent/futures/_base.py", line 449, in result
    return self.__get_result()
           ^^^^^^^^^^^^^^^^^^^
  File "/home/user/.conda/envs/model/lib/python3.11/concurrent/futures/_base.py", line 401, in __get_result
    raise self._exception
  File "/home/user/.conda/envs/model/lib/python3.11/concurrent/futures/thread.py", line 58, in run
    result = self.fn(*self.args, **self.kwargs)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/user/.conda/envs/model/lib/python3.11/site-packages/eolearn/core/eodata_io.py", line 162, in <lambda>
    new_files = list(executor.map(lambda saver: saver(), data_for_saving))
                                                ^^^^^^^
  File "/home/user/.conda/envs/model/lib/python3.11/site-packages/eolearn/core/eodata_io.py", line 607, in save
    cls._save(data, tempfs, "tmp_feature", compress_level)
  File "/home/user/.conda/envs/model/lib/python3.11/site-packages/eolearn/core/eodata_io.py", line 623, in _save
    cls._write_to_file(data, gzip_file, path)
  File "/home/user/.conda/envs/model/lib/python3.11/site-packages/eolearn/core/eodata_io.py", line 676, in _write_to_file
    return data.to_file(file, driver="GPKG", encoding="utf-8", layer=layer, index=False)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/user/.conda/envs/model/lib/python3.11/site-packages/geopandas/geodataframe.py", line 1536, in to_file
    _to_file(self, filename, driver, schema, index, **kwargs)
  File "/home/user/.conda/envs/model/lib/python3.11/site-packages/geopandas/io/file.py", line 686, in _to_file
    _to_file_pyogrio(df, filename, driver, schema, crs, mode, metadata, **kwargs)
  File "/home/user/.conda/envs/model/lib/python3.11/site-packages/geopandas/io/file.py", line 748, in _to_file_pyogrio
    pyogrio.write_dataframe(df, filename, driver=driver, metadata=metadata, **kwargs)
  File "/home/user/.conda/envs/model/lib/python3.11/site-packages/pyogrio/geopandas.py", line 548, in write_dataframe
    write(
  File "/home/user/.conda/envs/model/lib/python3.11/site-packages/pyogrio/raw.py", line 530, in write
    ogr_write(
  File "pyogrio/_io.pyx", line 1799, in pyogrio._io.ogr_write
  File "pyogrio/_io.pyx", line 1612, in pyogrio._io.ogr_create
pyogrio.errors.DataSourceError: sqlite3_open(<gzip _io.BufferedWriter name=b'/home/user/temp/tmpcowhph8o__tempfs__/tmp_feature' 0x7213b77aaa10>) failed: unable to open database file
<< end error

Just doing the task without save seems to succeed, the log file shows the expected columns of IDENTITIES after import, or the created HEIGHT raster

test 1
  vector_timeless={
    IDENTITIES: geopandas.GeoDataFrame(columns=['sample', 'NAME', 'HEIGHT-CM', 'ROW-TYPE', 'HULLESS-CONDITION', 'SBLOTCH-RATING', 'WEIGHT', 'DIAMETER', 'AREA', 'STEM-WEIGHT', 'DENSITY', 'ROWS', 'BARLEY-WHEAT', 'HULLED', 'SBLOTCH-LMH', 'geometry'], length=1212, crs=EPSG:32614)
  }

test 2
  data_timeless={
    HEIGHT: numpy.ndarray(shape=(500, 500, 1), dtype=float32)
  }

But on save of either the error is thrown.
checking the previously empty output dir I see that the eopatch_<n> has been created which includes a dir for the feature I am trying to create. this dir will be empty. Although it seems that permissions are not an issue

The env has been installed with conda, and to my reading satisfies dependencies of pyogrio
(model) conda list | grep pyogrio
pyogrio                   0.7.2           py311hbac4ec9_0    conda-forge
(model) conda list | grep gdal
gdal                      3.7.2           py311h815a124_1    conda-forge
libgdal                   3.7.2                h323ed7e_1    conda-forge
(model) conda list | grep sqlite
libsqlite                 3.49.1               hee588c1_2    conda-forge
sqlite                    3.49.1               h9eae976_2    conda-forge
(model) conda list | grep geopandas
geopandas                 1.0.1              pyhd8ed1ab_3    conda-forge
geopandas-base            1.0.1              pyha770c72_3    conda-forge
(model) conda list | grep shapely
shapely                   2.0.2           py311he06c224_0    conda-forge

I have searched your repo for calls to pyogrio and found nothing.
I have made a series of changes, in my system, moving to other disks, ensuring the program is being opened from the same disk etc.
Searches for this error string show very few relevant hits.
Is this something you have seen before? I am at my wits end. Your deeper experience in spatial data may suggest ways ahead to solve this.
'''


'''
made permissions on input and output dirs 777 to preclude read write permissions blocking sqlite.
got this new warning in the repl, execution reports remain the same.

Warning 1: File /vsimem/c38884637f984cad8f1708e508ccf941 has GPKG application_id, but non conformant file extension
/home/user/.conda/envs/model/lib/python3.11/site-packages/eolearn/geometry/transformations.py:178: EORuntimeWarning: Given vector polygons contain some invalid geometries, attempting to fix
  warnings.warn("Given vector polygons contain some invalid geometries, attempting to fix", EORuntimeWarning)
Warning 1: The filename extension should be 'gpkg' instead of '' to conform to the GPKG specification.
ERROR 4: sqlite3_open(<gzip _io.BufferedWriter name=b'/home/user/temp/tmpnx44la8p__tempfs__/tmp_feature' 0x7eddacd4d7b0>) failed: unable to open database file





'''

# &&& installed versions

######### ** Visualize layers
#####
# *** Object contents
# EOPATCH_TRAIN_DIR= os.path.join(DATA_OP_ROOT, "eopatches")
eopatch = EOPatch.load(os.path.join(EOPATCH_TRAIN_DIR, 'eopatch_2'))
eopatch
eopatch.timestamps
data_keys = sorted(list(eopatch.data.keys()))
data_keys

#####
# *** RGB per time
eopatch.plot((FeatureType.DATA, data_keys[3]))
#&&& make vis on load to check
#####
# *** Reference identities map
eopatch.plot((FeatureType.MASK_TIMELESS, 'IN_POLYGON'))
eopatch.plot((FeatureType.DATA_TIMELESS, 'HEIGHT'))
# *** Rasterized observations
################
# * Prepare eopatch
################
######### ** Concatenate
######### ** Erosion
######### ** Sampling
################
# * Create training data
################
######### ** Extract eopatch
######### ** Reshape data
#####
# *** Split patches into train test for GBM
#####
# *** Shape for GBM
# from t,w,h,f to n, m
# where n is pixels ie. w*h&&& and m is features x timesteps
#####
# *** Split samples into train test for TST &&&
#####
# *** Shape for TSP
# from t,w,h,f to s,v,t
# where s is w*h, v is features, t is time
################
# * GBM experiment
################
######### ** Train
######### ** Validate
#####
# *** F1 etc table
#####
# *** Confusion matrices
#####
# *** Class balance
#####
# *** ROC and AUC
#####
# *** Feature importance
######### ** Predict
#####
# *** visualize prediction
######### ** Quantify prediction
#####
# *** Visualize predicted trait
#####
# *** Visualize trait diff
#####
# *** Quantify agreement
################
# * TST experiment
################
######### ** Train
#####
# *** Unsupervised training
#####
# *** Transfer learning
######### ** Validate
#####
# *** F1 etc table
#####
# *** Confusion matrices
#####
# *** Class balance
#####
# *** ROC and AUC
#####
# *** Feature importance
######### ** Predict
#####
# *** visualize prediction
######### ** Quantify prediction
#####
# *** Visualize predicted trait
#####
# *** Visualize trait diff
#####
# *** Quantify agreement




############################################

class MultiLoader(EOTask):
    "Not used but kept because it was working although the shape was wrong"
    def __init__(self, dates, indices, sigmas):
        self.dates = dates
        self.indices = indices
        self.sigmas = sigmas
    def _toDatetime(self, stringlist):
        fmt = '%Y-%m-%d'
        return [datetime.datetime.strptime(s, fmt) for s in stringlist]
    def _toNParray(self, tiffs):
        # Open and stack the tiffs
        tiff_stack = []
        for path in tiffs:
            with rasterio.open(path) as src:
                tiff_stack.append(src.read(1))  # Read the first band
        # Convert to numpy array and reshape
        tiff_array = np.array(tiff_stack)
        # &&& modify channel dimension targeting t,h,w,c where c is 1
        reshaped_array = np.expand_dims(tiff_array, axis=-1)
        # - Before: `tiff_array.shape = (t, height, width)`
        # - After: `tiff_array.shape = (t, height, width, 1)`
        # &&& confirm this
        return reshaped_array
    def _make_safe_name(self, n, i, s):
        # pad the counter to maintain ordering
        zeroPaddedN = f"{n:0{4}d}"
        unsafe = f"{zeroPaddedN}_index_{i}_sigma_{s}"
        # need to drop decimals,invalid in feature names
        safe = unsafe.replace('.', '-')
        return safe
    def execute(self, bbox):
        eopatch = EOPatch(bbox=bbox)
        eopatch.timestamps = self._toDatetime(self.dates)
        eopatch.bbox = bbox
        n = 0 # make counter and add to name to force ordering
        for i in self.indices:
            for s in self.sigmas:
                name = self._make_safe_name(n, i, s)
                tiffs = select_tif_set(self.dates, [i], [s])
                array = self._toNParray(tiffs)
                eopatch[FeatureType.DATA, name] = array
                n = n + 1
        return eopatch

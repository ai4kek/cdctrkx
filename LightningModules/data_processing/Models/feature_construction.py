import os
import logging
from typing import IO
import numpy as np
from torch_sparse.tensor import to
import uproot as up

from functools import partial
from tqdm.contrib.concurrent import process_map

from ..feature_store_base import FeatureStoreBase
from ..utils.event_utils  import prepare_event

class TrackMLFeatureStore(FeatureStoreBase):
    """
    Processing model to convert STT data into files ready for GNN training
    
    Description:
        This class is used to read data from csv files containing PANDA STT hit and MC truth information.
        This information is then used to create true and input graphs and saved in PyTorch geometry files.
        It is a subclass of FeatureStoreBase which in turn is a subclass of the PyTorch Lighting's LightningDataModule.
    """
    
    def __init__(self, hparams : dict):
        """
        Initializes the TrackMLFeatureStore class.

        Args:
            hparams (dict): Dictionary containing the hyperparameters for the feature construction / data processing
        """

        # Call the base class (FeatureStoreBase in feature_store_base.py) constructor with the hyperparameters as arguments
        super().__init__(hparams)

        # self.detector_path = self.hparams["detector_path"]

    def prepare_data(self):
        """
        Main function for the feature construction / data processing.
        
        Description:
            Parallelizes the processing of input files by splitting them into 
            evenly sized chunks and processing each chunk in parallel.
        """

        # Create the output directory if it does not exist
        logging.info("Writing outputs to " + self.output_dir)
        os.makedirs(self.output_dir, exist_ok=True)

        # Check if the input file is a ROOT file by examining the extension
        fileExtention = os.path.splitext(self.input_dir)[1]

        if fileExtention == ".root":
            logging.info("Importing data from ROOT file.")
            self.useRootFile = True
            
            # Open the input ROOT file and get the number of events saved in the file
            try:
                with up.open(self.input_dir) as input_file:
                    inputRootFile = input_file
                    totEvents = input_file["hits"].num_entries
                    logging.info(f"Total number of events in the file: {totEvents}")
            except IOError:
                logging.error(f"Could not open the input file: {self.input_dir}")
            
            # Get the number of events to process
            if "n_files" not in self.hparams.keys():
                nEvents = totEvents
            else:
                nEvents = self.hparams["n_files"]
            
            logging.info(f"Number of events to process: {nEvents}")

            # make iterable of events
            all_events = [str(evt) for evt in list(range(nEvents))]

            # Process the input file with a worker pool and progress bar
            process_func = partial(prepare_event, inputRootFile=inputRootFile, **self.hparams )
            process_map(process_func, all_events, max_workers=self.n_workers, chunksize=self.chunksize)

        else:
            logging.info("Importing data from CSV files.")
            # Find the input files
            all_files = os.listdir(self.input_dir)
            all_events = sorted(np.unique([os.path.join(self.input_dir, event[:15]) for event in all_files]))[: self.n_files]

            # Split the input files by number of tasks and select my chunk only
            all_events = np.array_split(all_events, self.n_tasks)[self.task]

            # Process input files with a worker pool and progress bar
            # Use process_mp() from tqdm instead of mp.Pool from multiprocessing.
            process_func = partial(prepare_event, **self.hparams)
            process_map(process_func, all_events, max_workers=self.n_workers, chunksize=self.chunksize)
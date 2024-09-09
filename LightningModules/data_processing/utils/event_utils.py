#!/usr/bin/env python
# coding: utf-8

"""The module contains useful functions for handling data at the event level.
More fine-grained utilities are reserved for detector_utils and cell_utils."""

# TODO: Pull module IDs out into a csv file for readability

import os
import logging
import itertools
import torch
import trackml.dataset

import scipy as sp
import numpy as np
import pandas as pd

from torch_geometric.data import Data
from .heuristic_utils import get_layerwise_graph, get_all_edges, graph_intersection

# Device
device = 'cuda' if torch.cuda.is_available() else 'cpu'


def get_time_ordered_true_edges(hits):
    """Constructs the true edges using the MC Point time information for the hits.
    
    Args:
        hits (_type_): _description_
    
    Returns:
        _type_: The true edges in both directions
    """
    
    true_edges_start = []
    true_edges_end = []
    
    # Sort by the time of the MC Points
    for particle_id in hits.particle_id.unique():
        sortedHits = hits.query(f'particle_id=={particle_id}').sort_values('tT', ascending=True)
        for hit in range(sortedHits.x.size-1):
            true_edges_start.append(sortedHits.index[hit])
            true_edges_end.append(sortedHits.index[hit+1])
    
    true_edges = np.array([true_edges_start, true_edges_end])
    
    # Add the reverse edges
    true_edges = np.concatenate((true_edges, true_edges[[1, 0]]), axis=1)
    
    return true_edges


def get_layerwise_edges(hits):
    """Get layerwise true edge list. Here 'hits' represent complete event."""
    
    # Sort by increasing distance from production (IP)
    hits = hits.assign(
        R=np.sqrt(
            (hits.x - hits.vx) ** 2 + (hits.y - hits.vy) ** 2 + (hits.z - hits.vz) ** 2
        )
    )
    hits = hits.sort_values("R").reset_index(drop=True).reset_index(drop=False)
    
    # Handle noise (particle_id == 0)
    hits.loc[hits["particle_id"] == 0, "particle_id"] = np.nan
    
    # Group by particles and layers
    hit_list = (
        hits.groupby(["particle_id", "layer_id"], sort=False)["index"]
        .agg(lambda x: list(x))
        .groupby(level=0)
        .agg(lambda x: list(x))
    )
    
    # Build true edges
    true_edges = []
    for row in hit_list.values:
        for i, j in zip(row[0:-1], row[1:]):
            true_edges.extend(list(itertools.product(i, j)))
    
    # Convert and Transform
    true_edges = np.array(true_edges).T
    return true_edges, hits


def get_modulewise_edges(hits):
    """Get modulewise (layerless) true edge list using the order
    of hits based on R. Here 'hits' represent complete event."""
    
    # Handle NaN and Null Values
    signal = hits[
        ((~hits.particle_id.isna()) & (hits.particle_id != 0)) & (~hits.vx.isna())
    ]
    signal = signal.drop_duplicates(
        subset=["particle_id", "volume_id", "layer_id", "module_id"]
    )
    
    # Sort by increasing distance from production (IP)
    signal = signal.assign(
        R=np.sqrt(
            (signal.x - signal.vx) ** 2
            + (signal.y - signal.vy) ** 2
            + (signal.z - signal.vz) ** 2
        )
    )
    signal = signal.sort_values("R").reset_index(drop=False)
    
    # Handle re-indexing
    signal = signal.rename(columns={"index": "unsorted_index"}).reset_index(drop=False)
    
    # Handle noise (particle_id == 0)
    signal.loc[signal["particle_id"] == 0, "particle_id"] = np.nan
    
    # Group by particles
    signal_list = signal.groupby(["particle_id"], sort=False)["index"].agg(
        lambda x: list(x)
    )
    
    # Build true edges
    true_edges = []
    for row in signal_list.values:
        for i, j in zip(row[:-1], row[1:]):
            true_edges.append([i, j])
    
    # Convert and Transform
    true_edges = np.array(true_edges).T
    true_edges = signal.unsorted_index.values[true_edges]
    return true_edges


def get_orderwise_edges(hits):
    """Get modulewise (layerless) true edge list using the order
    of occurrence of hits. Here 'hits' represent complete event."""
    
    # Handle NaN and Null Values    
    signal = hits[
        ((~hits.particle_id.isna()) & (hits.particle_id != 0)) & (~hits.vx.isna())
    ]
    signal = signal.drop_duplicates(
        subset=["particle_id", "volume_id", "layer_id", "module_id"]
    )
    
    # Reset index to get 'index' column
    signal = signal.reset_index(drop=False)
    
    # Handle re-indexing
    # signal = signal.rename(columns={"index": "unsorted_index"}).reset_index(drop=False)
    
    # Handle Particle_id 0
    signal.loc[signal["particle_id"] == 0, "particle_id"] = np.nan
    
    # Group by particles
    signal_list = signal.groupby(["particle_id"], sort=False)["index"].agg(
        lambda x: list(x)
    )
    
    # Build true edges
    true_edges = []
    for row in signal_list.values:
        for i, j in zip(row[:-1], row[1:]):
            true_edges.append([i, j])
    
    # Convert and Transform
    true_edges = np.array(true_edges).T
    
    # Restore the Order
    # true_edges = signal.unsorted_index.values[true_edges]
    
    return true_edges


def process_particles(particles):
    """Special manipulation on particles data frame. One can exclude a particular 
    particle type to treat it as a noise. For example, if 'signal' is protons & pions
    then every other particle will be treated as a noise, pass 'signal' from config."""
    
    # select signal
    signal = [-2212, 2212, -211, 211]  # in future pass signal from config file
    particles = particles[particles['pdgcode'].isin(signal)].reset_index(drop=True)
    return particles


def select_hits(event_prefix, file_reader, read_truth, noise, skewed, **kwargs):
    """Hit selection method from Exa.TrkX. Build a full event, select hits based on certain criteria."""
    
    # select a data source (ROOT, CSV)
    if file_reader is not None:
        hits, tubes, particles, truth = file_reader.load_event(int(event_prefix), read_truth=read_truth)
        event_id = int(event_prefix)  # extract event_id

        logging.info(
            "Loading event {} from ROOT data source".format(
                event_prefix
            )
        )
    else:
        hits, tubes, particles, truth = trackml.dataset.load_event(event_prefix)
        event_id = int(event_prefix[-10:])  # extract event_id
        
        logging.info(
            "Loading event {} from CSV data source".format(
                event_prefix
            )
        )
    
    # store original order (needed for orderwise_true_edges function)
    hits['original_order'] = hits.index

    # preprocess 'particles' to get nhits, and drop duplicates
    particles['nhits'] = particles.groupby(['particle_id'])['nhits'].transform('count')
    particles.drop_duplicates(inplace=True, ignore_index=True)
    
    # apply particle selection (not nice, but works)
    if kwargs["selection"]:
        particles = process_particles(particles)
    
    # skip noise hits.
    if noise:
        # runs if noise=True
        truth = truth.merge(
            particles[["particle_id", "vx", "vy", "vz", "pdgcode"]], on="particle_id", how="left"
        )
    else:
        # runs if noise=False
        truth = truth.merge(
            particles[["particle_id", "vx", "vy", "vz", "pdgcode"]], on="particle_id", how="inner"
        )
    
    # derive new quantities from truth
    px = truth.tpx
    py = truth.tpy
    pz = truth.tpz

    # calculate pt, ptheta, peta, pphi
    pt = np.sqrt(px**2 + py**2)
    ptheta = np.arctan2(pt, pz)
    peta = -np.log(np.tan(0.5 * ptheta))
    pphi = np.arctan2(py, px)
    
    # assign pt, ptheta, peta, pphi to truth
    truth = truth.assign(pt=pt, ptheta=ptheta, peta=peta, pphi=pphi)
    
    # merge some columns of tubes to the hits
    hits = hits.merge(tubes[["hit_id", "isochrone", "skewed", "sector_id"]], on="hit_id")
    
    # skip skewed tubes
    if skewed is False:
        hits = hits.query('skewed==0')
    
    # Calculate derived variables from 'hits'
    r = np.sqrt(hits.x**2 + hits.y**2)               # Transverse distance from the interaction point
    phi = np.arctan2(hits.y, hits.x)                 # Azimuthal angle
    r3 = np.sqrt(hits.x**2 + hits.y**2 + hits.z**2)  # 3D distance from the interaction point
    theta = np.arccos(hits.z / r3)                   # Polar angle
    eta = -np.log(np.tan(theta / 2.))                # Pseudo-rapidity
    
    # Add r, phi, theta, eta to 'hits' and merge with 'truth'
    hits = hits.assign(r=r, phi=phi, theta=theta, eta=eta).merge(truth, on="hit_id")
    
    # Check if Order is Changed
    # assert (hits['original_order'] == hits.index).all(), "Order disturbed after merging with truth"
    
    # Restore the original order
    hits = hits.sort_values(by='original_order').reset_index(drop=True)
    
    # Drop the original_order column as it is no longer needed
    hits = hits.drop(columns=['original_order'])
    
    # Add 'event_id' column to this event.
    hits = hits.assign(event_id=int(event_prefix[-10:]))
    return hits


def build_event(
        event_prefix, file_reader, feature_scale,
        layerwise, modulewise, orderwise, timeOrdered, inputedges,
        noise, skewed, **kwargs):
    """Builds the event data by loading the event file and preprocessing the hit's data.
    
    Args:
        event_prefix (str): The path to the event file.
        file_reader (str): The data source of the event file.
        feature_scale (List): The scale factor for the features.
        layerwise (bool, optional): Whether to build the layerwise true edges (default: True).
        modulewise (bool, optional): Whether to build the modulewise true edges (default: False).
        orderwise (bool, optional): Whether to build the orderwise true edges (default: False).
        timeOrdered (bool, optional): Whether to build the timewise true edges (default: False).
        inputedges (bool, optional): Whether to build the input edges (default: True).
        noise (bool, optional): Whether to include noise hits in the data (default: False).
        skewed (bool, optional): Whether to include skewed tubes in the data (default: False).
        **kwargs: Additional keyword arguments to be passed to the select_hits function.
    
    Returns:
        pandas.DataFrame: The preprocessed hits data containing the event_id column.
    
    Note:
        This function first calls the select_hits function to load the hits data and preprocess it.
        Then it calculates the derived variables from the hits data.
        Next, it merges the hits data with the truth data and adds the event_id column.
        Finally, it returns the preprocessed hits data.
    """
    
    # Load event using "event_file" prefix (load_event function transferred to select_hits function).
    # hits, tubes, particles, truth = trackml.dataset.load_event(event_file)
    
    # Select hits, add new/select columns, add event_id
    hits = select_hits(event_prefix, file_reader,
                       read_truth=True, noise=noise, skewed=skewed,
                       **kwargs)
    
    # Get list of all layers
    layers = hits.layer_id.to_numpy()
    
    # Handle which truth graph(s) are being produced
    modulewise_true_edges, layerwise_true_edges = None, None
    orderwise_true_edges, time_ordered_true_edges = None, None
    
    # Get true edge list using the ordering of layers
    if layerwise:
        layerwise_true_edges, hits = get_layerwise_edges(hits)
        logging.info(
            "Layerwise truth graph built for {} with size {}".format(
                event_prefix, layerwise_true_edges.shape
            )
        )
    
    # Get true edge list without layer ordering
    if modulewise:
        modulewise_true_edges = get_modulewise_edges(hits)
        logging.info(
            "Modulewise truth graph built for {} with size {}".format(
                event_prefix, modulewise_true_edges.shape
            )
        )
    
    # Get true edge list without layer ordering (natural order)
    if orderwise:
        orderwise_true_edges = get_orderwise_edges(hits)
        logging.info(
            "Orderwise truth graph built for {} with size {}".format(
                event_prefix, orderwise_true_edges.shape
            )
        )
    
    # Get true edge list without layer ordering (time ordered)
    if timeOrdered:
        time_ordered_true_edges = get_time_ordered_true_edges(hits)
        logging.info(
            "Time ordered truth graph built for {} with size {}".format(
                event_prefix, time_ordered_true_edges.shape
                )
        )
    
    # Handle whether input graph(s) are being produced
    input_edges = None
    
    # Get input edge list using order of layers.
    if inputedges == "oldLayerwise":
        input_edges = get_layerwise_graph(hits, filtering=kwargs['filtering'], inneredges=False)  # w/o samelayer edges
        logging.info(
            "Layerwise input graph built for {} with size {}".format(
                event_prefix, input_edges.shape
                )
        )
    
    elif inputedges == "newLayerwise":
        input_edges = get_layerwise_graph(hits, filtering=kwargs['filtering'], inneredges=True)  # with samelayer edges
        logging.info(
            "Layerwise input graph built for {} with size {}".format(
                event_prefix, input_edges.shape
                )
        )
    
    elif inputedges == "all":
        input_edges = get_all_edges(hits)
        logging.info(
            "All input graph built for {} with size {}".format(
                event_prefix, input_edges.shape
                )
        )
    
    else:
        logging.error(f"{inputedges} is not a valid method to build input graphs")
        exit(1)

    # No weights of tracks in STT data yet, skipping it.
    # Get edge weight
    # edge_weights = (
    #    hits.weight.to_numpy()[modulewise_true_edges]
    #    if modulewise
    #    else hits.weight.to_numpy()[layerwise_true_edges]
    # )
    # edge_weight_average = (edge_weights[0] + edge_weights[1]) / 2
    # edge_weight_norm = edge_weight_average / edge_weight_average.mean()

    logging.info("Weights are not constructed, no weights for STT")
    
    return (
        hits[["r", "phi", "isochrone"]].to_numpy() / feature_scale,
        hits.particle_id.to_numpy(),
        layers,
        layerwise_true_edges,
        modulewise_true_edges,
        orderwise_true_edges,
        time_ordered_true_edges,
        input_edges,
        hits["hit_id"].to_numpy(),
        hits.pt.to_numpy(),
        hits[["vx", "vy", "vz"]].to_numpy(),
        hits.pdgcode.to_numpy(),
        hits.ptheta.to_numpy(),
        hits.peta.to_numpy(),
        hits.pphi.to_numpy()
    )


def prepare_event(
        event_prefix, file_reader, output_dir,
        layerwise, modulewise, orderwise, timeOrdered, inputedges,
        noise, skewed, overwrite, **kwargs):
    """Main function for processing an event.

    Description:
        This function collects the hit, truth, edge and edge label information and saves them into a PyG file.

    Args:
        event_prefix (str): Name of the event file
        file_reader (str): The data source of the event file
        output_dir (str): Directory in which to save the processed event
        layerwise (bool, optional): Whether to build the layerwise true edges (default: True)
        modulewise (bool, optional): Whether to build the modulewise true edges (default: False)
        orderwise (bool, optional): Whether to build the orderwise true edges (default: False)
        timeOrdered (bool, optional): Whether to build the timewise true edges (default: False)
        inputedges (bool, optional): Whether to build the input edges (default: True)
        noise (bool, optional): _description_. Defaults to False
        skewed (bool, optional): _description_. Defaults to False
        overwrite (bool, optional): _description_. Defaults to False
        **kwargs: Additional keyword arguments to be passed to the select_hits function
    """

    try:
        if file_reader is not None:
            
            # feature scale for X=[r,phi,z]
            feature_scale = [100, np.pi, 100]
            
            # build event
            (
                X,
                pid,
                layers,
                layerwise_true_edges,
                modulewise_true_edges,
                orderwise_true_edges,
                time_ordered_true_edges,
                input_edges,
                hid,
                pt,
                vertex,
                pdgcode,
                ptheta,
                peta,
                pphi
            ) = build_event(
                event_prefix=event_prefix,
                file_reader=file_reader,
                feature_scale=feature_scale,
                layerwise=layerwise,
                modulewise=modulewise,
                orderwise=orderwise,
                timeOrdered=timeOrdered,
                inputedges=inputedges,
                noise=noise,
                skewed=skewed,
                **kwargs
            )
            filename = os.path.join(output_dir, event_prefix)
        
        else:
            evtid = int(event_prefix[-10:])
            filename = os.path.join(output_dir, str(evtid))
            
            if not os.path.exists(filename) or overwrite:
                logging.info("Preparing event {}".format(evtid))
                
                # feature scale for X=[r,phi,z]
                feature_scale = [100, np.pi, 100]
                
                # build event
                (
                    X,
                    pid,
                    layers,
                    layerwise_true_edges,
                    modulewise_true_edges,
                    orderwise_true_edges,
                    time_ordered_true_edges,
                    input_edges,
                    hid,
                    pt,
                    vertex,
                    pdgcode,
                    ptheta,
                    peta,
                    pphi
                ) = build_event(
                    event_prefix=event_prefix,
                    file_reader=file_reader,
                    feature_scale=feature_scale,
                    layerwise=layerwise,
                    modulewise=modulewise,
                    orderwise=orderwise,
                    timeOrdered=timeOrdered,
                    inputedges=inputedges,
                    noise=noise,
                    skewed=skewed,
                    **kwargs
                )
            else:
                logging.info("{} already exists".format(evtid))
        
        # build PyTorch Geometric (PyG) 'Data' object
        data = Data(
            x=torch.from_numpy(X).float(),
            pid=torch.from_numpy(pid),
            layers=torch.from_numpy(layers),
            hid=torch.from_numpy(hid),
            pt=torch.from_numpy(pt),
            vertex=torch.from_numpy(vertex),
            pdgcode=torch.from_numpy(pdgcode),
            ptheta=torch.from_numpy(ptheta),
            peta=torch.from_numpy(peta),
            pphi=torch.from_numpy(pphi),
            event_file=event_prefix
        )
        
        # add true edges to PyTorch Geometric (PyG) 'Data' object
        if layerwise_true_edges is not None:
            data.layerwise_true_edges = torch.from_numpy(layerwise_true_edges)
        
        if modulewise_true_edges is not None:
            data.modulewise_true_edges = torch.from_numpy(modulewise_true_edges)
        
        if orderwise_true_edges is not None:
            data.orderwise_true_edges = torch.from_numpy(orderwise_true_edges)
        
        if time_ordered_true_edges is not None:
            data.time_ordered_true_edges = torch.from_numpy(time_ordered_true_edges)

        # NOTE: I am jumping from Processing to GNN stage, so I need ground truth (GT) of input
        # edges (edge_index). After embedding, one gets GT as y, and after filtering one gets
        # the GT in the form of 'y_pid'. As I intend to skip both the Embedding & the Filtering
        # stages, the input graph and its GT is build in Processing stage. The GNN can run after
        # either embedding or filtering stages, so it look for either 'y' or 'y_pid', existence of
        # one of these means the execution of these stages i.e. if y_pid exists in data that means
        # both embedding and filtering stages has been executed. If only 'y' exists then only
        # embedding stage has been executed. In principle, I should've only one of these in 'Data'.
        #
        # Now, for my case, I will build input graph during Processing and also add its GT to the
        # data. If the 'edge_index' is build in Processing then ground truth (y or y_pid) should
        # also be built here. The dimension of y (n) and y_pid (m) are given below, here m < n.
        #
        # y (n): after embedding along with e_radius (2,n), y.shape==e_radius.shape[1]
        # y_pid (m): after filtering along with e_radius (2,m), y_pid.shape==e_radius.shape[1]

        # add input edges to PyTorch Geometric (PyG) 'Data' object
        if input_edges is not None:
            # select true edges
            if layerwise:
                true_edges = data.layerwise_true_edges
            elif modulewise:
                true_edges = data.modulewise_true_edges
            elif orderwise:
                true_edges = data.orderwise_true_edges
            elif timeOrdered:
                true_edges = data.time_ordered_true_edges
            else:
                true_edges = None
            
            assert true_edges is not None
            
            # get input graph
            input_edges = torch.from_numpy(input_edges)
            new_input_edges, y = graph_intersection(input_edges, true_edges)
            data.edge_index = new_input_edges
            # data.y = y     # if regime: [] will point to embedding
            data.y_pid = y  # if regime: [[pid]] points to filtering
        
        # add cell/tube information to Data, Check for STT
        # logging.info("Getting cell info")
        
        # if cell_information:
        #    data = get_cell_information(
        #        data, cell_features, detector_orig, detector_proc, endcaps, noise
        #    )
        
        with open(filename, "wb") as pickle_file:
            torch.save(data, pickle_file)
    
    except Exception as inst:
        logging.error("File:", event_prefix, "had exception", inst)

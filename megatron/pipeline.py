import os
import numpy as np
import pandas as pd
import dill as pickle
from collections import defaultdict
from . import utils
from . import io


class Pipeline:
    """A pipeline with nodes as Transformations and InputNodes, edges as I/O relationships.

    Pipelines internally implement intelligent caching for maximal data re-use.
    Pipelines can also be saved with metadata intact for future use.

    Parameters
    ----------
    cache_dir : str (default: '../feature_cache')
        the relative path from the current working directory to store numpy data results
        for particular executions of nodes.

    Attributes
    ----------
    cache_dir : str
        the relative path from the current working directory to store numpy data results
        for particular executions of nodes.
    eager : bool
        when True, TransformationNode outputs are to be calculated on creation. This is indicated by
        data being passed to an InputNode node as a function call.
    nodes : list of TransformationNode / InputNode
        all InputNode and TransformationNode nodes belonging to the Pipeline.
    """
    def __init__(self, inputs, outputs, name=None, storage='local'):
        self.eager = False

        # flatten inputs into list of nodes
        self.inputs = []
        inputs = utils.generic.listify(inputs)
        for node in inputs:
            if utils.generic.isinstance_str(node, 'FeatureSet'):
                self.inputs += node.nodes
            else:
                self.inputs.append(node)

        # flatten outputs into list of nodes
        self.outputs = []
        outputs = utils.generic.listify(outputs)
        for node in outputs:
            if utils.generic.isinstance_str(node, 'FeatureSet'):
                self.outputs += node.nodes
            else:
                self.outputs.append(node)

        # ensure all outputs are named
        if any(node.is_default_name for node in self.outputs):
            msg = "All outputs must be named; passed as second parameter when Layer is called"
            raise NameError(msg)

        # calculate path from input to output
        self.path = utils.pipeline.topsort(self.outputs)

        # ensure input data matches with input nodes
        missing_inputs = set(self.path).intersection(self.inputs) - set(self.inputs)
        if len(missing_inputs) > 0:
            raise utils.errors.DisconnectedError(missing_inputs)
        extra_inputs = set(self.path).intersection(self.inputs) - set(self.path)
        if len(extra_inputs) > 0:
            utils.errors.ExtraInputsWarning(extra_inputs)

        # setup output data storage
        self.name = name
        if storage == 'local':
            self.storage = io.storage.LocalStorage(self.name)
        elif storage == 's3':
            self.storage = io.storage.S3Storage(self.name)

    def _reload(self):
        for node in self.path:
            node.has_run = False

    def _load_inputs(self, input_data):
        inputs_loaded = 0
        num_inputs = sum(1 for node in self.path if utils.generic.isinstance_str(node, 'InputNode'))
        for node in self.path:
            if utils.generic.isinstance_str(node, 'InputNode'):
                node.load(input_data[node.name])
                inputs_loaded += 1
            if inputs_loaded == num_inputs:
                break

    def _fit(self, input_data, partial):
        self._reload()
        self._load_inputs(input_data)
        for index, node in enumerate(self.path):
            if utils.generic.isinstance_str(node, 'TransformationNode'):
                try:
                    if partial:
                        node.partial_fit()
                    else:
                        node.fit()
                except Exception as e:
                    print("Error thrown at node named {}".format(node.name))
                    raise
            # erase data from nodes once unneeded (including output nodes)
            for predecessor in self.path[:index]:
                if all(out_node.has_run for out_node in predecessor.outbound_nodes):
                    predecessor.output = None
        # erase last node
        self.path[-1].output = None
        # restore has_run
        for node in self.path:
            node.has_run = False

    def _transform(self, input_data, cache_result):
        """Execute all non-cached nodes along the path given input data.

        Can cache the result for a path if requested.

        Parameters
        ----------
        input_data : dict of Numpy array
            the input data to be passed to InputNode TransformationNodes to begin execution.
        cache_result : bool
            whether to store the resulting Numpy arrays in the cache.

        Returns
        -------
        np.ndarray
            the data for the target node of the path given the input data.
        """
        self._reload()

        # run just the InputNode nodes to get the data hashes
        self._load_inputs(input_data)

        # run transformation nodes to end of path
        for index, node in enumerate(self.path):
            if utils.generic.isinstance_str(node, 'TransformationNode'):
                try:
                    if node.output is None:  # could be cache-loaded TransformationNode
                        node.transform()
                except Exception as e:
                    print("Error thrown at node named {}".format(node.name))
                    raise
            # erase data from nodes once unneeded
            for predecessor in self.path[:index]:
                outbound_run = all(out_node.has_run for out_node in predecessor.outbound_nodes)
                if outbound_run and predecessor not in self.outputs:
                    predecessor.output = None

    def partial_fit(self, input_data):
        self._fit(input_data, True)

    def fit(self, input_data):
        self._fit(input_data, False)

    def fit_generator(self, input_generator):
        for batch in input_generator:
            self.partial_fit(batch)

    def transform(self, input_data, cache_result=True, out_type='array'):
        """Execute the graph with some input data, get the output nodes' data.

        Parameters
        ----------
        input_data : dict of Numpy array
            the input data to be passed to InputNode TransformationNodes to begin execution.
        cache_result : bool
            whether to store the resulting Numpy array in the cache.
        form : {'array', 'dataframe'}
            data type to return as. If dataframe, colnames are node names.
        """
        if self.eager:
            raise utils.errors.EagerRunError()

        self._transform(input_data, cache_result)
        output_data = {node.name: node.output for node in self.outputs}
        self.storage.write(input_data, output_data)
        return utils.pipeline.format_output(output_data, out_type)

    def transform_generator(self, input_generator, cache_result=True, out_type='array'):
        for batch in input_generator:
            yield self.transform(batch, cache_result, out_type)

    def save(self, filepath):
        """Store just the nodes without their data (i.e. pre-execution) in a given file.

        Parameters
        ----------
        filepath : str
            the desired location of the stored nodes, filename included.
        """
        # TODO: make this more like Keras by outputting a JSON description of the model structure

        # store ref to data outside of Pipeline and remove ref to data in TransformationNodes
        data = {}
        for node in self.path:
            data[node] = node.output
            node.output = None
        with open(filepath, 'wb') as f:
            # keep same cache_dir too for new pipeline when loaded
            pipeline_info = {'nodes': self.path, 'cache_dir': self.cache_dir}
            pickle.dump(pipeline_info, f)
        # reinsert data into Pipeline
        for node in self.path:
            node.output = data[node]


def load_pipeline(filepath):
    """Load a set of nodes from a given file, stored previously with Pipeline.save().

    Parameters
    ----------
    filepath : str
        the file from which to load a Pipeline.
    """
    with open(filepath, 'rb') as f:
        pipeline_info = pickle.load(f)
    G = Pipeline(cache_dir=pipeline_info['cache_dir'])
    for node in pipeline_info['nodes']:
        G._add_node(node)
    return G
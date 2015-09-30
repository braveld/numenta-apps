# ----------------------------------------------------------------------
# Numenta Platform for Intelligent Computing (NuPIC)
# Copyright (C) 2015, Numenta, Inc.  Unless you have purchased from
# Numenta, Inc. a separate commercial license for this software code, the
# following terms and conditions apply:
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU Affero Public License for more details.
#
# You should have received a copy of the GNU Affero Public License
# along with this program.  If not, see http://www.gnu.org/licenses.
#
# http://numenta.org/licenses/
# ----------------------------------------------------------------------

"""
Implements Unicorn's model interface.
"""
import os
import sys

# Update pyproj datadir to point to the frozen directory
# See http://cx-freeze.readthedocs.org/en/latest/faq.html#using-data-files
if getattr(sys, "frozen", False):
  os.environ["PROJ_DIR"] = os.path.join(os.path.dirname(sys.executable), 
                                        "pyproj", "data")

from datetime import datetime
import json
import logging
from optparse import OptionParser
import pkg_resources
import traceback

import validictory

from nupic.algorithms.anomaly_likelihood import AnomalyLikelihood
from nupic.data import fieldmeta
from nupic.data import record_stream
from nupic.frameworks.opf.modelfactory import ModelFactory

# TODO: can we reuse htmengine primitives here?
from htmengine.algorithms.modelSelection.clusterParams import (
  getScalarMetricWithTimeOfDayParams)



g_log = logging.getLogger(__name__)

# TODO: figure out where our logs will go (should probably be a file since we
# use all stdio pipes for the protocol interface).
g_log.addHandler(logging.NullHandler())



class _CommandLineArgError(Exception):
  """ Error parsing command-line options """
  pass



class _Options(object):
  """Options returned by _parseArgs"""


  __slots__ = ("modelId", "stats",)


  def __init__(self, modelId, stats):
    """
    :param str modelId: model identifier
    :param dict stats: Metric data stats per stats_schema.json in the
      unicorn_backend package
    """
    self.modelId = modelId
    self.stats = stats



def _parseArgs():
  """ Parse command-line args

  :rtype: _Options object
  :raises _CommandLineArgError: on command-line arg error
  """
  class MyOptionParser(OptionParser):
    def error(self, msg):
      """Override `error()` to prevent unstructured output to stderr"""
      raise _CommandLineArgError(msg)

  helpString = (
    "%prog [options]\n\n"
    "Start Unicorn ModelRunner that runs a single model.")

  parser = MyOptionParser(helpString)

  parser.add_option(
    "--model",
    action="store",
    type="string",
    dest="modelId",
    help="Required: Model id string")

  parser.add_option(
    "--stats",
    action="store",
    type="string",
    dest="stats",
    help=("Required: see unicorn_backend/stats_schema.json"))


  options, positionalArgs = parser.parse_args()

  if len(positionalArgs) != 0:
    parser.error("Command accepts no positional args")

  if not options.modelId:
    parser.error("Missing or empty --modelId option value")

  if not options.stats:
    parser.error("Missing or empty --stats option value")

  stats = json.loads(options.stats)

  try:
    validictory.validate(
      stats,
      json.load(pkg_resources.resource_stream(__name__, "stats_schema.json")))
  except validictory.ValidationError as ex:
    parser.error("--stats option value failed schema validation: %r" % (ex,))


  return _Options(modelId=options.modelId, stats=stats)



class _ModelRunner(object):
  """ Use OPF Model to process metric data samples from stdin and and emit
  anomaly likelihood results to stdout
  """

  # Input column meta info compatible with parameters generated by
  # getScalarMetricWithTimeOfDayParams
  # of htmengine.algorithms.selection.clusterParams
  _INPUT_RECORD_SCHEMA = (
    fieldmeta.FieldMetaInfo("c0", fieldmeta.FieldMetaType.datetime,
                            fieldmeta.FieldMetaSpecial.timestamp),
    fieldmeta.FieldMetaInfo("c1", fieldmeta.FieldMetaType.float,
                            fieldmeta.FieldMetaSpecial.none),
  )


  def __init__(self, modelId, stats):
    """
    :param str modelId: model identifier
    :param dict stats: Metric data stats per stats_schema.json in the
      unicorn_backend package.
    """
    self._modelId = modelId

    # NOTE: ModelRecordEncoder is implemented in the pull request
    # https://github.com/numenta/nupic/pull/2432 that is not yet in master.
    self._modelRecordEncoder = record_stream.ModelRecordEncoder(
      fields=self._INPUT_RECORD_SCHEMA)

    self._model = self._createModel(stats=stats)

    self._anomalyLikelihood = AnomalyLikelihood()


  @classmethod
  def _createModel(cls, stats):
    """Instantiate and configure an OPF model

    :param dict stats: Metric data stats per stats_schema.json in the
      unicorn_backend package.
    :returns: OPF Model instance
    """
    # Generate swarm params
    possibleModels = getScalarMetricWithTimeOfDayParams(
      metricData=[0],
      minVal=stats["min"],
      maxVal=stats["max"],
      minResolution=stats.get("minResolution"))

    swarmParams = possibleModels[0]

    model = ModelFactory.create(modelConfig=swarmParams["modelConfig"])
    model.enableLearning()
    model.enableInference(swarmParams["inferenceArgs"])

    return model


  @classmethod
  def _readInputMessages(cls):
    """Create a generator that waits for and yields input messages from
    stdin

    yields two-tuple (<timestamp>, <scalar-value>), where <timestamp> is the
    `datetime.datetime` timestamp of the metric data sample and <scalar-value>
    is the floating point value of the metric data sample.
    """
    while True:
      message = sys.stdin.readline()

      if message:
        timestamp, scalarValue = json.loads(message)
        yield (datetime.utcfromtimestamp(timestamp), scalarValue)
      else:
        # Front End closed the pipe (or died)
        break


  @classmethod
  def _emitOutputMessage(cls, rowIndex, anomalyProbability):
    """Emit output message to stdout

    :param int rowIndex: 0-based index of corresponding input sample
    :param float anomalyProbability: computed anomaly probability value
    """
    message = "%s\n" % (json.dumps([rowIndex, anomalyProbability]),)

    sys.stdout.write(message)
    sys.stdout.flush()


  def _computeAnomalyProbability(self, inputRow):
    """ Compute anomaly likelihood score

    :param tuple inputRow: Two-tuple input metric data row
      (<datetime-timestamp>, <float-scalar>)

    :returns: Anomaly probability
    :rtype: float
    """
    # Generate raw anomaly score
    inputRecord = self._modelRecordEncoder.encode(inputRow)
    rawAnomalyScore = self._model.run(inputRecord).inferences["anomalyScore"]

    # Generate anomaly likelihood score
    anomalyProbability = self._anomalyLikelihood.anomalyProbability(
      value=inputRow[1],
      anomalyScore=rawAnomalyScore,
      timestamp=inputRow[0])

    return anomalyProbability


  def run(self):
    """ Run the model: ingest and process the input metric data and emit output
    messages containing anomaly scores
    """
    g_log.info("Processing model=%s", self._modelId)

    for rowIndex, inputRow in enumerate(self._readInputMessages()):
      anomalyProbability = self._computeAnomalyProbability(inputRow)

      self._emitOutputMessage(rowIndex=rowIndex,
                              anomalyProbability=anomalyProbability)



def main():
  try:

    options = _parseArgs()

    _ModelRunner(modelId=options.modelId, stats=options.stats).run()

  except Exception as ex:  # pylint: disable=W0703
    g_log.exception("ModelRunner failed")

    errorMessage = {
      "errorText": str(ex) or repr(ex),
      "diagnosticInfo": traceback.format_exc()
    }

    errorMessage = "%s\n" % (json.dumps(errorMessage))

    try:
      sys.stderr.write(errorMessage)
      sys.stderr.flush()
    except Exception:  # pylint: disable=W0703
      g_log.exception("Failed to emit error message to stderr; msg=%s",
                      errorMessage)

    # Use os._exit to abort the process instead of an exception to prevent
    # the python runtime from dumping traceback to stderr (since we dump a json
    # message to stderr, and don't want the extra text to interfere with parsing
    # in the Front End)
    os._exit(1)  # pylint: disable=W0212



if __name__ == "__main__":
  main()

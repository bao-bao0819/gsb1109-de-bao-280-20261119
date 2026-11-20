#  Copyright 2013 Lars Butler & individual contributors
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
__version__ = '1.1.0'


class InvalidGeoJSONException(Exception):
    """
    Simple exception class to indicate if invalid GeoJSON is encountered.
    """


class InvalidWKTException(ValueError):
    """
    A WKT value failed strict parsing.

    Strict parsing requires a cell to contain exactly one complete WKT
    geometry, optionally surrounded by leading and trailing whitespace.
    Unlike the lenient reader, trailing content or an incomplete geometry
    are never silently ignored.

    ``position``, ``line`` and ``column`` refer to the original, unmodified
    cell text (no trimming, line splitting or SRID stripping), so data
    suppliers can locate the offending character in the field they sent.

    Subclasses :class:`ValueError` so callers which already catch
    ``ValueError`` keep working unchanged.
    """

    def __init__(self, message, text=None, position=None, line=None,
                 column=None):
        super(InvalidWKTException, self).__init__(message)
        self.text = text
        self.position = position
        self.line = line
        self.column = column

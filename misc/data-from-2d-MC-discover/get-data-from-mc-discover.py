#!/usr/bin/env python
import functools
import io
import json
import os
import subprocess
import sys
import urllib
import urllib.parse
import urllib.request

from typing import Optional, Dict, List, Tuple

import numpy as np

sys.path.append("../..")
from compute.phononweb.qephonon_qetools import (  # pylint: disable=wrong-import-position
    QePhononQetools,
)

MATDYN_EXECUTABLE = os.path.expanduser("~/git/q-e/bin/matdyn.x")


class MissingPhononsError(Exception):
    """Exception raised if no phonons exist for a given compound."""


def _prettify_string(name):
    pretty_chars = []
    for char in name:
        if char in "0123456789":
            pretty_chars.append(f"<sub>{char}</sub>")
        else:
            pretty_chars.append(char)
    return "".join(pretty_chars)


def prettify_formula(formula, prototype):
    ret_string = _prettify_string(formula)
    if prototype:
        ret_string += f" [{_prettify_string(prototype)}]"
    return ret_string


def check_matdyn():
    process = subprocess.run(
        [MATDYN_EXECUTABLE],
        input="",
        check=False,
        capture_output=True,
        encoding="ascii",
    )
    # matdyn.x could create a CRASH file
    try:
        os.remove("CRASH")
    except FileNotFoundError:
        pass

    header_lines = [
        line.strip()
        for line in process.stdout.splitlines()
        if line.strip().startswith("Program MATDYN")
    ]
    if not header_lines:
        raise AssertionError("Could not find the expected header line in matdyn run...")
    header_line = header_lines[0]
    version = header_line.split()[2]
    # print("Matdyn version:", version, "NOTE: you need a recent 6.x version to support the 2D cutoff!")
    # For now I just do a stupid check, this would need to be improved.
    # I am not really sure in which version it the 2D cutoff was implemented - probably
    # in 6.1, while in 6.0 it's not there. Feel free to add more versions if you know it's working
    # (or more recent versions)
    assert version in [
        "v.6.8",
        "v.6.7.0",
        "v.6.7MaX",
    ], f"Version '{version}' not supported, if you know it works add it to the list of supported versions"


def get_matdyn_input_file(
    high_symmetry_points: List, high_symmetry_points_coordinates: List
) -> Tuple[str, List]:
    matdyn_input_file = """&INPUT
asr = 'simple'
loto_2d = .true.
fldos = ''
flfrc = 'real_space_force_constants.dat'
flfrq = ''
flvec = 'matdyn.modes'
q_in_cryst_coord = .true.
q_in_band_form = .true.
/
"""

    # For now I recompute also lines that should be 'skipped' (e.g. if the band has Y|A, I also compute the
    # Y-A segment). Currently the point is skipped (I could do it) but the phonon visualizer will still display
    # a line (with straight segments) of the length of the Y-A segment, with no selectable points, that is worse
    # (as people might think that there are 'straight' phonon bands).
    # I also hardcode the length of the paths to be 20 points as a compromise between smoothness and file size.
    new_matdyn_lines = []
    current_point_cnt = 0
    final_high_sym_kpts = []
    for (_, kpt_label), kpt_coords in zip(
        high_symmetry_points, high_symmetry_points_coordinates
    ):
        num_points_this_segment = 20
        new_matdyn_lines.append(
            f"{kpt_coords[0]:18.10f} {kpt_coords[1]:18.10f} {kpt_coords[2]:18.10f} {num_points_this_segment}"
        )
        if kpt_label == "G":
            kpt_label = "Γ"
        final_high_sym_kpts.append([current_point_cnt, kpt_label])
        current_point_cnt += num_points_this_segment

    matdyn_input_file += f"{len(new_matdyn_lines)}\n"
    matdyn_input_file += "\n".join(new_matdyn_lines)
    matdyn_input_file += "\n"
    return matdyn_input_file, final_high_sym_kpts


def needs_node(f):
    """Decorator for methods to validate early that a node is loaded."""

    @functools.wraps(f)
    def wrapper(self, *args, **kwargs):
        """Print an error and return if there is no node loaded."""
        if not self.current_uuid:
            raise ValueError("A node must be loaded first with `load_node`")
        return f(self, *args, **kwargs)

    return wrapper


class AiiDARestResponse:
    def __init__(self, raw_response: bytes):
        """Get the raw bytes from the response, parses them and offers them in an intuitive way."""
        self._raw_data: Dict = json.loads(raw_response)

    @property
    def raw_data(self) -> Dict:
        return self._raw_data

    @property
    def data(self) -> Dict:
        return self.raw_data["data"]

    @property
    def path(self) -> str:
        return self.raw_data["path"]

    @property
    def resource_type(self) -> str:
        return self.raw_data["resource_type"]

    @property
    def url(self) -> str:
        return self.raw_data["url"]

    @property
    def method(self) -> str:
        return self.raw_data["method"]

    @property
    def query_string(self) -> str:
        return self.raw_data["query_string"]

    @property
    def url_root(self) -> str:
        return self.raw_data["url_root"]


class NodesAiiDARestResponse(AiiDARestResponse):
    def __init__(self, raw_response: bytes):
        super().__init__(raw_response=raw_response)
        assert "nodes" in self.data

    @property
    def nodes(self) -> List:
        return self.data["nodes"]


class SingleNodeAiiDARestResponse(NodesAiiDARestResponse):
    def __init__(self, raw_response: bytes):
        super().__init__(raw_response=raw_response)
        if len(self.nodes) == 0:
            raise ValueError("No nodes found in the response")
        if len(self.nodes) > 1:
            raise ValueError("More than one node returned in the response")

    @property
    def node(self) -> Dict:
        return self.nodes[0]


class AiiDARestClient:
    def __init__(self, endpoint: str):
        """
        Creates the REST client.

        The endpoint should be something like:

            https://aiida.materialscloud.org/2dstructures/api/v4
        """
        # endpoint without final slash (if present)
        self._endpoint = endpoint[:-1] if endpoint.endswith("/") else endpoint
        self._current_uuid: Optional[str] = None

    def _make_request(self, request_url: str) -> bytes:
        return urllib.request.urlopen(request_url).read()

    @property
    def current_uuid(self) -> Optional[str]:
        return self._current_uuid

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    @needs_node
    def node_url(self) -> str:
        return f"{self.endpoint}/nodes/{self.current_uuid}"

    def load_node(self, uuid: str) -> None:
        self._current_uuid = uuid

    def unload_node(self) -> None:
        self._current_uuid = None

    @needs_node
    def get_node_metadata(self):
        return SingleNodeAiiDARestResponse(self._make_request(self.node_url))

    @needs_node
    def get_node_repo_content(self, filename) -> bytes:
        url = f"{self.node_url}/repo/contents?filename=%22{urllib.parse.quote(filename)}%22"
        return self._make_request(url)

    @needs_node
    def get_node_repo_list(self) -> List:
        url = f"{self.node_url}/repo/list"
        return AiiDARestResponse(self._make_request(url)).data["repo_list"]

    @needs_node
    def get_attributes(self) -> Dict:
        url = f"{self.node_url}?attributes=true"
        response = SingleNodeAiiDARestResponse(self._make_request(url))
        return response.node["attributes"]

    @needs_node
    def get_incoming(self) -> List:
        url = f"{self.node_url}/links/incoming"
        response = AiiDARestResponse(self._make_request(url))
        return response.data["incoming"]

    @needs_node
    def get_incoming_dict(self) -> Dict:
        incoming = self.get_incoming()
        incoming_dict = {inc["link_label"]: inc for inc in incoming}
        if len(incoming) != len(incoming_dict):
            raise ValueError(
                "More than one incoming link with the same label. You need to use this method only if you know that the incoming labels are unique"
            )
        return incoming_dict

    @needs_node
    def get_outgoing(self) -> List:
        url = f"{self.node_url}/links/outgoing"
        response = AiiDARestResponse(self._make_request(url))
        return response.data["outgoing"]

    @needs_node
    def get_outgoing_dict(self) -> Dict:
        outgoing = self.get_outgoing()
        outgoing_dict = {out["link_label"]: out for out in outgoing}
        if len(outgoing) != len(outgoing_dict):
            raise ValueError(
                "More than one outgoing link with the same label. You need "
                "to use this method only if you know that the outgoing labels "
                "are unique, e.g. for AiiDA processes"
            )
        return outgoing_dict


def get_files_from_materials_cloud(
    discover_data, compound
):  # pylint: disable=too-many-locals, too-many-statements
    """Given a compound name, return the content of some relevant files (as a dictionary)."""
    compounds = discover_data["data"]["compounds"]
    material = compounds[compound]
    try:
        phonons_uuid = material["phonons_2D"]
    except KeyError:
        raise MissingPhononsError(f"No phonons for {compound}")

    # Create REST client, check that I am pointing to a BandsData
    client = AiiDARestClient(
        endpoint="https://aiida.materialscloud.org/2dstructures/api/v4/"
    )
    client.load_node(phonons_uuid)
    assert (
        client.get_node_metadata().node["full_type"] == "data.array.bands.BandsData.|"
    )

    # Retrieve k-point coordinates and labels
    bands_kpts_array = np.load(io.BytesIO(client.get_node_repo_content("kpoints.npy")))
    attributes = client.get_attributes()
    high_symmetry_points = list(zip(attributes["label_numbers"], attributes["labels"]))
    # [((0, u'G'), array([ 0.,  0.,  0.])), ((49, u'M'), array([ 0.5,  0. ,  0. ])),
    #  ((85, u'K'), array([ 0.33333333,  0.33333333,  0.        ])), ((131, u'G'), array([ 0.,  0.,  0.]))]
    high_symmetry_points_coordinates = [
        bands_kpts_array[sym_kpt[0]] for sym_kpt in high_symmetry_points
    ]
    # Prepare matdyn input file
    matdyn_input_file, final_high_sym_kpts = get_matdyn_input_file(
        high_symmetry_points, high_symmetry_points_coordinates
    )

    # Retrieve phonon bands input (matdyn.x)
    client.load_node(
        client.get_incoming()[0]["uuid"]
    )  # Move to the creator: it's a single one
    matdyn_uuid = client.current_uuid

    # Go to the force constants FolderData
    client.load_node(client.get_incoming_dict()["parent_calc_folder"]["uuid"])
    force_constants_uuid = client.current_uuid
    real_force_constants = client.get_node_repo_content(
        "real_space_force_constants.dat"
    )

    ## Retrieve PW inputs and outputs
    # First go to the q2r calculation
    client.load_node(
        client.get_incoming()[0]["uuid"]
    )  # Move to the creator: it's a single one
    # Then go to the parent calc folder (RemoteData)
    client.load_node(client.get_incoming_dict()["parent_calc_folder"]["uuid"])
    # Now get the calculation itself - it's a calcfunction
    client.load_node(
        client.get_incoming()[0]["uuid"]
    )  # Move to the creator: it's a single one
    assert (
        client.get_node_metadata().node["node_type"]
        == "process.calculation.calcfunction.CalcFunctionNode."
    ), "The parent of the Q2R is not a CalcFunction as I expected!"

    # Go to the correct parent, depending on which link labels there is
    inputs_dict = client.get_incoming_dict()
    try:
        remote_data_uuid = inputs_dict["retrieved_1"][
            "uuid"
        ]  # Go to the ph.x of the first q-point
    except KeyError:
        # Some materials follow a different path via a different calcfunction, but
        # the parent of it is still a ph.x so I can just switch which input to follow
        remote_data_uuid = inputs_dict["ph_folder_with_eps"]["uuid"]
    client.load_node(remote_data_uuid)

    # Now get the parent - it might still be a phonon calculation
    client.load_node(
        client.get_incoming()[0]["uuid"]
    )  # Move to the creator: it's a single one

    # There might be multiple restarts - I iterate until I find a pw
    is_still_ph = True
    while is_still_ph:
        # I go up to the RemoteData and then to the creator
        client.load_node(client.get_incoming_dict()["parent_calc_folder"]["uuid"])
        client.load_node(
            client.get_incoming()[0]["uuid"]
        )  # Move to the creator: it's a single one

        scf_pw_uuid = client.current_uuid
        scf_input_file = client.get_node_repo_content("aiida.in")
        if b"&inputph" not in scf_input_file.lower():
            # I found a non-ph calculation (should be hopefully a PW). I go out of the loop.
            is_still_ph = False

    if b"calculation = 'scf'" not in scf_input_file:
        raise ValueError(
            f"The parent calculation does not seem to be a SCF for '{compound}', UUID={client.current_uuid}"
        )

    # Go to the retrieved SCF outputs
    client.load_node(client.get_outgoing_dict()["retrieved"]["uuid"])
    scf_output_file = client.get_node_repo_content("aiida.out")

    return {
        "files": {
            "scf.in": scf_input_file,
            "scf.out": scf_output_file,
            "real_space_force_constants.dat": real_force_constants,
            "matdyn.in": matdyn_input_file.encode("ascii"),
        },
        "uuids": {
            "scf": scf_pw_uuid,
            "matdyn": matdyn_uuid,
            "phonon_bands": phonons_uuid,
            "force_constants": force_constants_uuid,
        },
        "high_symmetry_points": final_high_sym_kpts,
    }


if __name__ == "__main__":

    check_matdyn()

    discover_url = (
        "https://www.materialscloud.org/mcloud/api/v2/discover/2dstructures/compounds"
    )
    discover_data = json.loads(urllib.request.urlopen(discover_url).read())

    # ["AgNO2", "Bi", "BN", "C", "PbI2", "MoS2-MoS2", "P", "PbTe"]:
    for compound in sorted(discover_data["data"]["compounds"].keys()):

        # Problematic cases:
        if compound in [
            # MISSING PHONONS
            "CeSiI",
            "DySBr",
            "DySI",
            "ErHCl",
            "ErSCl",
            "ErSeI",
            "NdOBr",
            "SmOBr",
            "SmSI",
            "TbBr",
            "TbCl",
            "TmOI",
            "YbOBr",
        ]:
            continue

        dest_folder = os.path.join("out-phonons", compound)

        ## Now I have all data, I create a folder and store all files
        ## Skip this material if the destination folder exists
        try:
            os.makedirs(dest_folder, exist_ok=False)
        except FileExistsError:
            if os.path.exists(os.path.join(dest_folder, os.pardir, f"{compound}.json")):
                print(
                    f"> Skipping '{compound}' as destination folder '{dest_folder}' exists."
                )
                continue
            print(
                f"ERROR: Stopping: folder '{dest_folder}' exists but ther is no JSON inside. Remove it to regenerate it."
            )
            sys.exit(1)

        compound_info = get_files_from_materials_cloud(discover_data, compound)

        # I write the content to files
        for filename, content in compound_info["files"].items():
            with open(os.path.join(dest_folder, filename), "wb") as fhandle:
                fhandle.write(content)
        with open(os.path.join(dest_folder, "uuids.json"), "w") as fhandle:
            json.dump(compound_info["uuids"], fhandle)
        print(f"Files written to folder '{dest_folder}'")

        current_dir = os.path.realpath(os.curdir)
        try:
            os.chdir(dest_folder)
            process = subprocess.run(
                [MATDYN_EXECUTABLE, "-in", "matdyn.in"],
                check=False,
                capture_output=True,
                encoding="ascii",
            )
            assert (
                "JOB DONE." in process.stdout
            ), f"matdyn.x mode did not finish correctly... Ouput:\n{process.stdout}"
            assert os.path.exists(
                "matdyn.modes"
            ), f"matdyn.x mode did not generate the matdyn.modes file... Ouput:\n{process.stdout}"
            with open("matdyn.modes") as fhandle:
                matdyn_modes = fhandle.read()
        finally:
            os.chdir(current_dir)

        print("matdyn.x run successfully, matdyn.modes generated.")

        phonons = QePhononQetools(
            scf_input=compound_info["files"]["scf.in"].decode("ascii"),
            scf_output=compound_info["files"]["scf.out"].decode("ascii"),
            matdyn_modes=matdyn_modes,
            highsym_qpts=compound_info["high_symmetry_points"],
            starting_reps=(5, 5, 1),
            reorder=True,
            name=prettify_formula(
                formula=discover_data["data"]["compounds"][compound]["formula"],
                prototype=discover_data["data"]["compounds"][compound]["prototype"],
            ),
        )

        json_fname = os.path.realpath(
            os.path.join(dest_folder, os.pardir, "{}.json".format(compound))
        )
        with open(json_fname, "w") as fhandle:
            data = phonons.get_dict()
            # Remove alat if defined (so there is no message about Quantum ESPRESSO when the JSON file is loaded)
            try:
                data.pop("alat")
            except KeyError:
                pass

            json.dump(data, fhandle)

        print(f"'{json_fname}' file written.")

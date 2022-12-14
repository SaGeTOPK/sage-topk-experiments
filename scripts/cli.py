import click
import json
import yaml
import csv
import os
import re
import glob
import logging
import hashlib
import urllib.parse

from pandas import DataFrame
from typing import Tuple, List

from spy import Spy
from approaches.factory import ApproachFactory


###############################################################################
# ### Helping functions
###############################################################################


def list_files(path: str) -> List[str]:
    if not os.path.isdir(path):
        return glob.glob(path)
    files = list()
    for filename in os.listdir(path):
        if filename.endswith(".sparql"):
            files.append(f"{path}/{filename}")
    return files


def load_queries(path: str) -> List[Tuple[str, str]]:
    queries = list()
    for file in list_files(path):
        with open(file, "r") as reader:
            filename = os.path.basename(file).split(".")[0]
            query = reader.read()
            queries.append((filename, query))
    return queries


def save_dataframe(dataframe: DataFrame, output: str, mode: str = "w") -> None:
    if output is not None:
        header = not (mode == "a" and os.path.exists(output))
        dataframe.to_csv(output, mode=mode, index=False, header=header)


def save_json(data: dict, output: str) -> None:
    if output is not None:
        with open(output, "w") as outfile:
            json.dump(data, outfile, indent=4)


###############################################################################
# ### Command-line interface
###############################################################################


@click.group()
def cli():
    pass


@cli.command()
@click.argument(
    "queryfile", type=click.Path(exists=True, file_okay=True, dir_okay=False))
@click.option(
    "--configfile",
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    default="config/watdiv.yaml")
@click.option(
    "--approach", type=click.Choice(ApproachFactory.types()), default="sage")
@click.option(
    "--limit", type=click.INT, default=0)
@click.option(
    "--max-limit", type=click.INT, default=10000)
@click.option(
    "--quota", type=click.INT, default=None)
@click.option(
    "--early-pruning", type=click.BOOL, default=False)
@click.option(
    "--stateless", type=click.BOOL, default=True)
@click.option(
    "--force-order/--default-ordering", default=False)
@click.option(
    "--stats", type=click.Path(exists=False), default=None)
@click.option(
    "--output", type=click.Path(exists=False), default=None)
@click.option(
    "--verbose/--quiet", default=False)
def topk_run(
    queryfile, configfile, approach, limit, max_limit, quota, early_pruning,
    stateless, force_order, stats, output, verbose
):
    if verbose:
        logging.basicConfig(
            level="INFO",
            format="%(asctime)s - %(message)s",
            datefmt="%m/%d/%Y %I:%M:%S")
    config = yaml.safe_load(stream=open(configfile, "r"))
    filename, query = load_queries(queryfile)[0]

    if "ORDER BY" not in query or "LIMIT" not in query:
        logging.info("Error: The query is not a TOP-k query...")
        return

    spy = Spy()  # used to collect statistics
    engine = ApproachFactory.create(approach, config)

    solutions = engine.execute_query(
        query, spy, limit=limit, max_limit=max_limit, quota=quota,
        early_pruning=early_pruning, stateless=stateless,
        force_order=force_order)
    dataframe = spy.to_dataframe()

    logging.info((
        f"{approach} - query executed in {spy.execution_time / 1000} seconds "
        f"with {len(solutions)} solutions"))
    if len(solutions) > 0:
        first_solution = json.dumps(solutions[0], indent=4)
        last_solution = json.dumps(solutions[-1], indent=4)
        logging.info(f"{approach} - first solution:\n{first_solution}")
        logging.info(f"{approach} - last solution:\n{last_solution}")
    logging.info(f"{approach} - dataframe:\n{dataframe}")

    save_json(solutions, output)
    save_dataframe(dataframe, stats)


@cli.command()
@click.argument(
    "reference", type=click.Path(exists=True, file_okay=True, dir_okay=False))
@click.argument(
    "actual", type=click.Path(exists=True, file_okay=True, dir_okay=False))
@click.option(
    "--output", type=click.Path(exists=False), default=None)
@click.option(
    "--verbose/--quiet", default=False)
def compare(reference, actual, output, verbose):
    if verbose:
        logging.basicConfig(
            level="INFO",
            format="%(asctime)s - %(message)s",
            datefmt="%m/%d/%Y %I:%M:%S")
    reference = json.load(open(reference, "r"))
    actual = json.load(open(actual, "r"))

    correct = True
    memory = {}
    for mappings in reference:
        sorted_mappings = {k: mappings[k] for k in sorted(mappings.keys())}
        key = hashlib.md5(str(sorted_mappings).encode("utf-8")).digest()
        if key not in memory:
            memory[key] = 0
        memory[key] += 1
    for mappings in actual:
        sorted_mappings = {k: mappings[k] for k in sorted(mappings.keys())}
        key = hashlib.md5(str(sorted_mappings).encode("utf-8")).digest()
        if key not in memory:
            logging.info(f"Incorrect solution: {sorted_mappings}")
            correct = False
            break
        memory[key] -= 1
        if memory[key] < 0:
            logging.info(f"Duplicated solution: {sorted_mappings}")
            correct = False
            break
    correct = all([value == 0 for value in memory.values()])

    if correct:
        logging.info("The TOP-K is correct")
    else:
        logging.info("The TOP-K is incorrect")
    save_dataframe(DataFrame([[correct]], columns=["correct"]), output)


@cli.command()
@click.argument(
    "queries", type=click.Path(exists=True, dir_okay=False, file_okay=True))
@click.argument(
    "output", type=click.Path(exists=True, dir_okay=True, file_okay=False))
@click.option(
    "--configfile",
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    default="config/watdiv.yaml")
def extract_queries(queries, output, configfile):
    config = yaml.safe_load(stream=open(configfile, "r"))
    engine = ApproachFactory.create("sage", config)

    nb_query = len(load_queries(output)) + 1

    expected = ["SELECT", "ORDER BY", "LIMIT"]
    rejected = [
        "OPTIONAL", "GROUP BY", "BIND", "DISTINCT", "RAND", "STRAFTER",
        "*", "|"]

    regex_service = re.compile(
        "((.*?\n)*?.*?)(SERVICE.*?ontology#label.*?{(.*?\n)*?.*?})")
    orderby_variables = re.compile("(ASC|DESC)\( (\?var[0-9])Label \)")

    with open(queries, 'r') as csvfile:
        rows = csv.reader(csvfile, delimiter='\t')
        header = None
        for index, row in enumerate(rows):
            if header is None:
                header = row
                continue
            query = urllib.parse.unquote_plus(row[0])
            if not all([operator in query for operator in expected]):
                continue
            if any([operator in query for operator in rejected]):
                continue
            query = re.sub(regex_service, "\\1", query)
            query = re.sub(orderby_variables, "\\1( \\2 )", query)
            print(f"Wikidata Query:\n{query}")
            answer = input("Compute number of solutions (y/n): ")
            if answer != "y":
                continue
            solutions = engine.execute_query(query, Spy(), limit=1000000)
            print(f"Number of solutions: {len(solutions)}")
            answer = input("Keep this query (y/n): ")
            if answer == "y":
                with open(f"{output}/Q{nb_query}.sparql", "w") as queryfile:
                    queryfile.write(query)
                print(f"Number of queries: {nb_query}")
                nb_query += 1


if __name__ == "__main__":
    cli()

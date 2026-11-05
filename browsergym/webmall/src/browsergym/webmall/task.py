import os
import logging
import json
import pdb

from dataclasses import asdict
from typing import Tuple
from playwright.sync_api import Page
from pathlib import Path
from .evaluator import RequestTracker, Evaluator, StringEvaluator, URLEvaluator, CartEvaluator, CheckoutEvaluator, EvaluatorComb
from .checkpoints import Checklist, Checkpoint

logger = logging.getLogger(__name__)

from browsergym.core.task import AbstractBrowserTask

PATH_TO_DOT_ENV_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "..", ".env")
)


class WebMallTask(AbstractBrowserTask):

    def __init__(
        self, task_id: str, seed: int = None, output_file: str = None, save_predictions: bool = False
    ) -> None:
        """
        Args:
            seed (int): Random seed for task initialization.
            task_id (str): Unique identifier for the task (for the BrowserGym environment).
            output_file (str, optional): Path to the output file for saving results, needed for test set.
            save_predictions (bool, optional): Save predictions to the output file (yes/no).
        """
        super().__init__(seed)
        self.task_id = task_id
        self.urls = self.read_env_file(PATH_TO_DOT_ENV_FILE)
        json_path = Path(__file__).parent / "task_sets.json"
        with open(json_path, 'r') as f:
            self.task_sets = json.load(f)

        self.task_config = _get_task(self.task_sets, task_id.split(".")[1])

        replacements = [
            ("{{URL_1}}", self.urls["SHOP1_URL"]),
            ("{{URL_2}}", self.urls["SHOP2_URL"]),
            ("{{URL_3}}", self.urls["SHOP3_URL"]),
            ("{{URL_4}}", self.urls["SHOP4_URL"]),
            ("{{URL_5}}", self.urls["FRONTEND_URL"]),
            ("Shop1", "E-Store Athletes"),
            ("Shop2", "TechTalk"),
            ("Shop3", "CamelCases"),
            ("Shop4", "Hardware Cafe")
        ]

        # For chekout tasks and end-to-end tasks, inject user details and payment info
        if self.task_config["category"] == "Checkout" or self.task_config["category"] == "FindAndOrder":
            # Add user details and payment info to replacements
            user_details = self.task_config.get("user_details", {})
            payment_info = self.task_config.get("payment_info", {})
            if user_details:
                replacements.append(("{{name}}", user_details.get("name", "")))
                replacements.append(("{{email}}", user_details.get("email", "")))
                replacements.append(("{{street}}", user_details.get("street", "")))
                replacements.append(("{{house_number}}", user_details.get("house_number", "")))
                replacements.append(("{{zip}}", user_details.get("zip", "")))
                replacements.append(("{{city}}", user_details.get("city", "")))
                replacements.append(("{{state}}", user_details.get("state", "")))
                replacements.append(("{{country}}", user_details.get("country", "")))
            if payment_info:
                replacements.append(("{{card}}", payment_info.get("card", "")))
                replacements.append(("{{cvv}}", payment_info.get("cvv", "")))
                replacements.append(("{{expiry_date}}", payment_info.get("expiry_date", "")))

        # For checkout tasks only, inject product url
        #if self.task_config["category"] == "Checkout":
        #    replacements.append(("{{product_url}}", self.task_config["correct_answer"]["answers"][0]))

        self.task_config = _replace_placeholders(self.task_config, replacements)

        self.checklist = Checklist(self.task_config, task_id=self.task_id, urls=self.urls)
    
    def get_task_id(self) -> str:
        """
        Generic class for several task ids, this way of obtaining the task id
        """
        return self.task_id

    def setup(self, page: Page) -> Tuple[str, dict]:
        start_url = self.urls['FRONTEND_URL']
        logger.info(f"Navigating to start url: {start_url}")
        page.goto(start_url, timeout=50000)
        self.request_tracker = RequestTracker(page.context)

        general_instruction = self.task_config.get("instruction", "")
        specific_instruction = self.task_config.get("task", "")
        general_instruction = general_instruction.replace('\\n', '\n')
        specific_instruction = specific_instruction.replace('\\n', '\n')

        return general_instruction + specific_instruction, {}

    def teardown(self) -> None:
        pass

    def validate(self, page: Page, chat_messages: list[dict]) -> Tuple[float, bool, str, dict]:
        if chat_messages and chat_messages[-1]["role"] == "assistant":
            last_message = {"answer": chat_messages[-1]["message"]}
        elif chat_messages and chat_messages[-1]["role"] == "infeasible":
            last_message = {"answer": "N/A"}
        else:
            last_message = {"answer": ""}

        self.request_tracker.wait_until_settled(page)
        evaluator = _evaluator_router(self.checklist)
        checkpoints_old = self.checklist.checkpoints.copy()
        score, self.checklist, wrong_solutions = evaluator.score(last_message, page, self.checklist)
        reached_this_step = self._get_checkpoints_reached_this_step(checkpoints_old)

        if "Done" in last_message["answer"] or evaluator.done:
            logger.info(f"Agent has terminated the task with a total score of {self.checklist.total_score()}.")
            logger.info(f"{len(self.checklist.get_checked_checkpoints())} out of {len(self.checklist.checkpoints)} checkpoints have been reached.")
            return score, True, "", {"checklist": self.checklist.get_checklist_dict(), "reached_during_this_step": reached_this_step, "wrong_solutions": wrong_solutions}
        elif chat_messages and chat_messages[-1]["role"] == "infeasible":
            return score, True, "", {"checklist": self.checklist.get_checklist_dict(), "reached_during_this_step": reached_this_step, "wrong_solutions": wrong_solutions}
        else:   
            return score, False, "", {"checklist": self.checklist.get_checklist_dict(), "reached_during_this_step": reached_this_step, "wrong_solutions": wrong_solutions}
    
    def read_env_file(self, file_path):
        """Reads environment variables from the system first,
        then falls back to a .env file if any are missing.

        Raises an error if required variables are not found in either.
        """
        required_vars = {"SHOP1_URL", "SHOP2_URL", "SHOP3_URL", "SHOP4_URL", "FRONTEND_URL"}

        # Step 1: Try reading from system environment
        env_vars = {var.strip().strip("'").strip('"'): os.getenv(var).strip().strip("'").strip('"') for var in required_vars if os.getenv(var) is not None}

        missing_vars = required_vars - env_vars.keys()

        if not missing_vars:
            return env_vars  # All required vars found in system environment

        # Step 2: Fallback to reading from .env file
        if os.path.exists(file_path):
            with open(file_path, "r") as file:
                for line in file:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue

                    key, sep, value = line.partition("=")
                    if sep and key.strip() in missing_vars:
                        env_vars[key.strip().strip("'").strip('"')] = value.strip().strip("'").strip('"')

            still_missing = required_vars - env_vars.keys()
            if still_missing:
                raise ValueError(
                    f"The .env file at '{file_path}' is missing required variables: {', '.join(still_missing)}.\n"
                    f"Please ensure the .env file is correctly configured."
                )

            return env_vars

        else:
            # .env file doesn't exist and system variables were incomplete
            script_path = os.path.abspath(__file__)
            raise FileNotFoundError(
                f"Some required environment variables are missing and no .env file was found at '{file_path}'.\n"
                f"Missing: {', '.join(missing_vars)}\n"
                f"Set the variables manually or ensure the .env file exists.\n"
                f"(Check script: {script_path})"
            )
    
    def _get_checkpoints_reached_this_step(self, checkpoints_old: list[Checkpoint]) -> list[dict]:
        checkpoints_new = self.checklist.checkpoints
        checked_new = [cp for cp in checkpoints_new if cp.flag]
        unchecked_old_ids = {cp.id for cp in checkpoints_old if not cp.flag}
        return [asdict(cp) for cp in checked_new if cp.id in unchecked_old_ids]


def _get_task(task_sets: list, task_id: str) -> dict | None:
    """
    Returns a specific task from the task set.
    """
    for task_set in task_sets:
        for task in task_set.get("tasks", []):
            if task.get("id") == task_id:
                return task
    return None

def _replace_placeholders(obj, replacements):
    if isinstance(obj, dict):
        return {k: _replace_placeholders(v, replacements) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_replace_placeholders(i, replacements) for i in obj]
    elif isinstance(obj, str):
        for placeholder, replacement in replacements:
            obj = obj.replace(placeholder, replacement)
        return obj
    else:
        return obj

def _evaluator_router(checklist: Checklist) -> EvaluatorComb:
    """Router to get one evaluator per type"""
    unchecked = checklist.get_unchecked_checkpoints()
    eval_types = set(cp.type for cp in unchecked)  # deduplicate types
    evaluators: list[Evaluator] = []

    for eval_type in eval_types:
        match eval_type:
            case "string":
                evaluators.append(StringEvaluator())
            case "checkout":
                evaluators.append(CheckoutEvaluator())
            case "cart":
                evaluators.append(CartEvaluator())
            case "url":
                evaluators.append(URLEvaluator())
            case _:
                raise ValueError(f"eval_type {eval_type} is not supported")
            
    # Ensure a StringEvaluator is always included (necessary for ending cart-related tasks).
    if not any(isinstance(evaluator, StringEvaluator) for evaluator in evaluators):
        evaluators.append(StringEvaluator())

    return EvaluatorComb(evaluators)


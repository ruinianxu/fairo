"""
Copyright (c) Facebook, Inc. and its affiliates.
"""

import logging
import os
import signal
import subprocess
import time
import boto3

from droidlet.tools.hitl.turk_oncall.allocate_oncall_instances import (
    allocate_oncall_instances,
    free_ecs_instances,
)
from droidlet.tools.hitl.utils.hitl_utils import (
    generate_batch_id,
    deregister_dashboard_subdomain,
)

from droidlet.tools.hitl.data_generator import DataGenerator
from droidlet.tools.hitl.job_listener import JobListener
from droidlet.tools.hitl.task_runner import TaskRunner


ECS_INSTANCE_TIMEOUT = 45
INTERACTION_JOB_POLL_TIME = 30
INTERACTION_LISTENER_POLL_TIME = 30
HITL_TMP_DIR = (
    os.environ["HITL_TMP_DIR"] if os.getenv("HITL_TMP_DIR") else f"{os.path.expanduser('~')}/.hitl"
)
S3_BUCKET_NAME = "droidlet-hitl"
S3_ROOT = "s3://droidlet-hitl"
NSP_OUTPUT_FNAME = "nsp_outputs"
ANNOTATED_COMMANDS_FNAME = "nsp_data.txt"

AWS_ACCESS_KEY_ID = os.environ["AWS_ACCESS_KEY_ID"]
AWS_SECRET_ACCESS_KEY = os.environ["AWS_SECRET_ACCESS_KEY"]
AWS_DEFAULT_REGION = os.environ["AWS_DEFAULT_REGION"]
s3 = boto3.resource(
    "s3",
    region_name=AWS_DEFAULT_REGION,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
)

log_formatter = logging.Formatter(
    "%(asctime)s [%(filename)s:%(lineno)s - %(funcName)s() %(levelname)s]: %(message)s"
)
logger = logging.getLogger()
logger.handlers.clear()
logger.setLevel("INFO")
sh = logging.StreamHandler()
sh.setFormatter(log_formatter)
logger.addHandler(sh)


class OnCallJob(DataGenerator):
    """
    This Data Generator is responsible for spinning up the Turk-As-Oncall version of Interaction Jobs.

    Each Interaction Job consists of several HITs. Turker are given dashboard sessions where they can
    send a set of predetermined commands to the craftassist agent and mark whether the response was as expected.

    On a high level:
    - The input of this data generator is a request specifying how many dashboard sessions are created for turkers
    - The output of this data generator is a set of dashboard logs with the results of each session.

    """

    def __init__(
        self, instance_num: int, image_tag: str, task_name: str, timeout: float = -1
    ) -> None:
        super(OnCallJob, self).__init__(timeout)
        self._instance_num = instance_num
        self._image_tag = image_tag
        self._task_name = task_name
        self.instance_ids = None
        self._batch_id = generate_batch_id()

    def run(self) -> None:
        batch_id = self._batch_id

        # allocate AWS ECS instances and register DNS records
        logging.info(
            f"Allocate AWS ECS instances, populate oncall data csv, and register DNS records..."
        )
        _, instance_ids = allocate_oncall_instances(
            self._instance_num, batch_id, self._image_tag, self._task_name, ECS_INSTANCE_TIMEOUT
        )
        self.instance_ids = instance_ids

        # run Mephisto to spin up & monitor turk jobs
        logging.info(f"Start running Mephisto...")
        MEPHISTO_AWS_ACCESS_KEY_ID = os.environ["MEPHISTO_AWS_ACCESS_KEY_ID"]
        MEPHISTO_AWS_SECRET_ACCESS_KEY = os.environ["MEPHISTO_AWS_SECRET_ACCESS_KEY"]
        MEPHISTO_REQUESTER = os.environ["MEPHISTO_REQUESTER"]
        p = subprocess.Popen(
            [
                f"echo -ne '\n' |  \
                    AWS_ACCESS_KEY_ID='{MEPHISTO_AWS_ACCESS_KEY_ID}' \
                    AWS_SECRET_ACCESS_KEY='{MEPHISTO_AWS_SECRET_ACCESS_KEY}' \
                    python ../../crowdsourcing/turk_as_oncall/static_run_with_qual.py \
                    mephisto.provider.requester_name={MEPHISTO_REQUESTER} \
                    mephisto.architect.profile_name=mephisto-router-iam"
            ],
            shell=True,
            preexec_fn=os.setsid,
        )

        # Keep running Mephisto until timeout or job finished
        while not self.check_is_timeout() and p.poll() is None:
            logging.info(
                f"[Oncall Job] Oncall Job still running...Remaining time: {self.get_remaining_time()}"
            )
            time.sleep(INTERACTION_JOB_POLL_TIME)

        # if mephisto is still running after job timeout, terminate it
        logging.info(f"Manually terminate Mephisto after timeout...")
        if p.poll() is None:
            os.killpg(os.getpgid(p.pid), signal.SIGINT)
            time.sleep(300)
            os.killpg(os.getpgid(p.pid), signal.SIGINT)
            time.sleep(300)
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)

        # Deregister DNS records
        logging.info(f"Deregister DNS records...")
        deregister_dashboard_subdomain(batch_id)

        logging.info(f"Free ECS instances...")
        free_ecs_instances(self.instance_ids)

        self.set_finished()

    def get_batch_id(self):
        return self._batch_id


if __name__ == "__main__":
    runner = TaskRunner()
    ocj = OnCallJob(instance_num=2, image_tag="oncall_v1", task_name="oncall_t2", timeout=30)
    batch_id = ocj.get_batch_id()
    runner.register_data_generators([ocj])
    runner.run()

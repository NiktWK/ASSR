"""MLflow REST adapter. Existing runs are found by stable ASSR run ID after a retry."""

import time

import httpx


class Tracking:
    def __init__(self, url=""):
        self.url = url.rstrip("/")

    def log(self, campaign, run):
        if not self.url:
            return None
        with httpx.Client(base_url=self.url + "/api/2.0/mlflow/", timeout=20) as c:

            def post(endpoint, payload):
                r = c.post(endpoint, json=payload)
                r.raise_for_status()
                return r.json()

            name = "assr-" + campaign["id"]
            found = c.get("experiments/get-by-name", params={"experiment_name": name})
            if found.status_code == 200:
                eid = found.json()["experiment"]["experiment_id"]
            else:
                eid = post("experiments/create", {"name": name})["experiment_id"]
            rid = run["spec"]["run_id"]
            existing = post(
                "runs/search",
                {"experiment_ids": [eid], "filter": f"tags.assr_run_id = '{rid}'", "max_results": 1},
            ).get("runs", [])
            if existing:
                mid = existing[0]["info"]["run_id"]
            else:
                mid = post(
                    "runs/create",
                    {
                        "experiment_id": eid,
                        "start_time": int(time.time() * 1000),
                        "tags": [
                            {"key": "assr_run_id", "value": rid},
                            {"key": "assr_mode", "value": campaign["project"]["mode"]},
                        ],
                    },
                )["run"]["info"]["run_id"]
            spec = run["spec"]
            params = [
                {"key": k, "value": str(spec[k])}
                for k in (
                    "commit_sha",
                    "source_digest",
                    "config_digest",
                    "dataset_digest",
                    "evaluator_digest",
                    "image_digest",
                    "variant",
                    "seed",
                )
            ]
            post(
                "runs/log-batch",
                {
                    "run_id": mid,
                    "params": params,
                    "metrics": [
                        {
                            "key": "accuracy",
                            "value": run["result"]["metrics"]["accuracy"],
                            "timestamp": int(time.time() * 1000),
                            "step": 0,
                        }
                    ],
                    "tags": [
                        {
                            "key": "assr_artifacts_uri",
                            "value": run["result"].get("artifacts_uri", "local-demo"),
                        }
                    ],
                },
            )
            post("runs/update", {"run_id": mid, "status": "FINISHED", "end_time": int(time.time() * 1000)})
            return mid

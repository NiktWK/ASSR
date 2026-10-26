from dataclasses import dataclass
import os
from pathlib import Path


@dataclass
class Settings:
    root: Path
    database_url: str = ""
    token: str = ""
    ssh_host: str = ""
    llm_url: str = ""
    llm_key: str = ""
    llm_model: str = ""
    input_rate: float = 0
    output_rate: float = 0
    mlflow_url: str = ""
    worker: bool = True

    def __post_init__(self):
        self.root = Path(self.root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.database_url:
            self.database_url = "sqlite:///" + str(self.root / "assr.db")

    @classmethod
    def env(cls):
        return cls(
            root=Path(os.environ.get("ASSR_ROOT", ".assr")),
            database_url=os.environ.get("ASSR_DATABASE_URL", ""),
            token=os.environ.get("ASSR_TOKEN", ""),
            ssh_host=os.environ.get("ASSR_SSH_HOST", ""),
            llm_url=os.environ.get("ASSR_LLM_URL", ""),
            llm_key=os.environ.get("ASSR_LLM_KEY", ""),
            llm_model=os.environ.get("ASSR_LLM_MODEL", ""),
            input_rate=float(os.environ.get("ASSR_INPUT_USD_PER_MILLION", "0")),
            output_rate=float(os.environ.get("ASSR_OUTPUT_USD_PER_MILLION", "0")),
            mlflow_url=os.environ.get("ASSR_MLFLOW_URL", ""),
            worker=os.environ.get("ASSR_WORKER", "1") == "1",
        )

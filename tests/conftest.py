from pathlib import Path
import pytest
from assr import demo_train
from assr.demo import setup
from assr.engine import Engine
from assr.settings import Settings


@pytest.fixture
def engine(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "train.py").write_text(Path(demo_train.__file__).read_text())
    settings = Settings(root=tmp_path / "state", worker=False)
    setup(settings.root, source)
    return Engine(settings)

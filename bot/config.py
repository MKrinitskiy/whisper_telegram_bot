import yaml
import dotenv
from pathlib import Path

config_dir = Path(__file__).parent.parent.resolve() / "config"

# load yaml config
with open(config_dir / "config.yml", 'r') as f:
    config_yaml = yaml.safe_load(f)

# load .env config
config_env = dotenv.dotenv_values(config_dir / "config.env")

# config parameters
telegram_token = config_yaml["telegram_token"]
allowed_telegram_usernames = config_yaml["allowed_telegram_usernames"]
n_transcription_langs_per_page = config_yaml.get("n_transcription_langs_per_page", 6)
mongodb_uri = f"mongodb://{config_env['MONGODB_HOSTNAME']}:{config_env['MONGODB_PORT']}"
# telegram_base_url = config_yaml["telegram_base_url"]

# transcription_langs
with open(config_dir / "transcription_languages.yml", 'r') as f:
    transcription_langs = yaml.safe_load(f)

from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # AWS
    aws_access_key_id:     str
    aws_secret_access_key: str
    aws_region:            str = "eu-west-1"
    collection_id:         str = "face-recognition-collection"

    # Flask API callback
    seats_api_url: str = "http://localhost:3000"   # swap to Railway URL in prod
    seats_api_key: str = ""

    class Config:
        env_file = ".env"

settings = Settings()

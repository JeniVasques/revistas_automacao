from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

creds = Credentials.from_service_account_file(
    "automacaoeditais-8f0db36dc8e9.json",
    scopes=SCOPES
)

creds.refresh(Request())

print("TOKEN GERADO COM SUCESSO")
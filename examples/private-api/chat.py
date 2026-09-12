"""Call the private chat API: python chat.py --ca-cert lan-test-root.crt"""
import argparse
import getpass
import os
import sys
from pathlib import Path

import requests


def main():
    parser = argparse.ArgumentParser(description="Send a question to the private assistant API.")
    parser.add_argument("--endpoint", default="https://192.168.0.111:8443/api/chat")
    parser.add_argument("--message", default="What services do you offer?")
    parser.add_argument("--conversation-id", default="python-test-001")
    parser.add_argument("--language", choices=("en", "sv"), default="en")
    parser.add_argument("--ca-cert", type=Path, help="Path to lan-test-root.crt for the LAN test server")
    args = parser.parse_args()

    if args.ca_cert and not args.ca_cert.is_file():
        parser.error("Certificate file does not exist: " + str(args.ca_cert))
    token = os.environ.get("ASSISTANT_API_TOKEN") or getpass.getpass("Private API token: ")
    if not token.strip():
        parser.error("A private API token is required.")

    try:
        response = requests.post(
            args.endpoint,
            headers={"Authorization": "Bearer " + token.strip()},
            json={
                "conversation_id": args.conversation_id,
                "message": args.message,
                "language": args.language,
            },
            verify=str(args.ca_cert) if args.ca_cert else True,
            timeout=(10, 90),
            allow_redirects=False,
        )
        if not 200 <= response.status_code < 300:
            print(f"Request failed: HTTP {response.status_code}", file=sys.stderr)
            return 1
        data = response.json()
        print(data.get("answer", data))
        for source in data.get("sources", []):
            print(f"Source: {source.get('title', '')} — {source.get('url', '')}")
        return 0
    except requests.exceptions.SSLError:
        print("Certificate verification failed. Supply the LAN root certificate with --ca-cert.", file=sys.stderr)
    except requests.exceptions.Timeout:
        print("The request timed out.", file=sys.stderr)
    except requests.exceptions.RequestException:
        print("Connection failed. Check the endpoint, network, and firewall.", file=sys.stderr)
    except ValueError:
        print("The server returned an invalid JSON response.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

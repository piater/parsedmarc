import logging
from enum import Enum
from functools import lru_cache
from time import sleep
from typing import List, Optional

from azure.identity import UsernamePasswordCredential, \
    DeviceCodeCredential, ClientSecretCredential
from msgraph.core import GraphClient

from parsedmarc.mail.mailbox_connection import MailboxConnection


class AuthMethod(Enum):
    DeviceCode = 1
    UsernamePassword = 2
    ClientSecret = 3


logger = logging.getLogger('parsedmarc')


def _generate_credential(auth_method: str, **kwargs):
    if auth_method == AuthMethod.DeviceCode.name:
        credential = DeviceCodeCredential(
            client_id=kwargs['client_id'],
            client_secret=kwargs['client_secret'],
            disable_automatic_authentication=True,
            tenant_id=kwargs['tenant_id']
        )
    elif auth_method == AuthMethod.UsernamePassword.name:
        credential = UsernamePasswordCredential(
            client_id=kwargs['client_id'],
            client_credential=kwargs['client_secret'],
            disable_automatic_authentication=True,
            username=kwargs['username'],
            password=kwargs['password']
        )
    elif auth_method == AuthMethod.ClientSecret.name:
        credential = ClientSecretCredential(
            client_id=kwargs['client_id'],
            tenant_id=kwargs['tenant_id'],
            client_secret=kwargs['client_secret']
        )
    else:
        raise RuntimeError(f'Auth method {auth_method} not found')
    return credential


class MSGraphConnection(MailboxConnection):
    def __init__(self,
                 auth_method: str,
                 mailbox: str,
                 client_id: str,
                 client_secret: str,
                 username: str,
                 password: str,
                 tenant_id: str):
        credential = _generate_credential(auth_method,
                                          client_id=client_id,
                                          client_secret=client_secret,
                                          username=username,
                                          password=password,
                                          tenant_id=tenant_id)
        scopes = ['Mail.ReadWrite']
        # Detect if mailbox is shared
        if username and mailbox and username != mailbox:
            scopes = ['Mail.ReadWrite.Shared']
        if not isinstance(credential, ClientSecretCredential):
            credential.authenticate(scopes=scopes)
        self._client = GraphClient(credential=credential)
        self.mailbox_name = mailbox

    def create_folder(self, folder_name: str):
        sub_url = ''
        path_parts = folder_name.split('/')
        if len(path_parts) > 1:  # Folder is a subFolder
            parent_folder_id = None
            for folder in path_parts[:-1]:
                parent_folder_id = self._find_folder_id_with_parent(
                    folder, parent_folder_id)
            sub_url = f'/{parent_folder_id}/childFolders'
            folder_name = path_parts[-1]

        request_body = {
            'displayName': folder_name
        }
        request_url = f'/users/{self.mailbox_name}/mailFolders{sub_url}'
        resp = self._client.post(request_url, json=request_body)
        if resp.status_code == 409:
            logger.debug(f'Folder {folder_name} already exists, '
                         f'skipping creation')
        elif resp.status_code == 201:
            logger.debug(f'Created folder {folder_name}')
        else:
            logger.warning(f'Unknown response '
                           f'{resp.status_code} {resp.json()}')

    def fetch_messages(self, folder_name: str) -> List[str]:
        """ Returns a list of message UIDs in the specified folder """
        folder_id = self._find_folder_id_from_folder_path(folder_name)
        url = f'/users/{self.mailbox_name}/mailFolders/' \
              f'{folder_id}/messages?$select=id'
        result = self._client.get(url)
        emails = result.json()['value']
        return [email['id'] for email in emails]

    def mark_message_read(self, message_id: str):
        """Marks a message as read"""
        url = f'/users/{self.mailbox_name}/messages/{message_id}'
        resp = self._client.patch(url, json={"isRead": "true"})
        if resp.status_code != 200:
            raise RuntimeWarning(f"Failed to mark message read"
                                 f"{resp.status_code}: {resp.json()}")

    def fetch_message(self, message_id: str):
        url = f'/users/{self.mailbox_name}/messages/{message_id}/$value'
        result = self._client.get(url)
        self.mark_message_read(message_id)
        return result.text

    def delete_message(self, message_id: str):
        url = f'/users/{self.mailbox_name}/messages/{message_id}'
        resp = self._client.delete(url)
        if resp.status_code != 204:
            raise RuntimeWarning(f"Failed to delete message "
                                 f"{resp.status_code}: {resp.json()}")

    def move_message(self, message_id: str, folder_name: str):
        folder_id = self._find_folder_id_from_folder_path(folder_name)
        request_body = {
            'destinationId': folder_id
        }
        url = f'/users/{self.mailbox_name}/messages/{message_id}/move'
        resp = self._client.post(url, json=request_body)
        if resp.status_code != 201:
            raise RuntimeWarning(f"Failed to move message "
                                 f"{resp.status_code}: {resp.json()}")

    def keepalive(self):
        # Not needed
        pass

    def watch(self, check_callback, check_timeout):
        """ Checks the mailbox for new messages every n seconds"""
        while True:
            sleep(check_timeout)
            check_callback(self)

    @lru_cache(maxsize=10)
    def _find_folder_id_from_folder_path(self, folder_name: str) -> str:
        path_parts = folder_name.split('/')
        parent_folder_id = None
        if len(path_parts) > 1:
            for folder in path_parts[:-1]:
                folder_id = self._find_folder_id_with_parent(
                    folder, parent_folder_id)
                parent_folder_id = folder_id
            return self._find_folder_id_with_parent(
                path_parts[-1], parent_folder_id)
        else:
            return self._find_folder_id_with_parent(folder_name, None)

    def _find_folder_id_with_parent(self,
                                    folder_name: str,
                                    parent_folder_id: Optional[str]):
        sub_url = ''
        if parent_folder_id is not None:
            sub_url = f'/{parent_folder_id}/childFolders'
        url = f'/users/{self.mailbox_name}/mailFolders{sub_url}'
        folders_resp = self._client.get(url)
        folders = folders_resp.json()['value']
        matched_folders = [folder for folder in folders
                           if folder['displayName'] == folder_name]
        if len(matched_folders) == 0:
            raise RuntimeError(f"folder {folder_name} not found")
        selected_folder = matched_folders[0]
        return selected_folder['id']

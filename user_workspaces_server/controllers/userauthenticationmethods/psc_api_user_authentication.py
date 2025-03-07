from user_workspaces_server.controllers.userauthenticationmethods.abstract_user_authentication import \
    AbstractUserAuthentication
from django.forms.models import model_to_dict
import json
from rest_framework.exceptions import ParseError, PermissionDenied
from rest_framework.authtoken.models import Token
import requests as http_r


class PSCAPIUserAuthentication(AbstractUserAuthentication):

    def __init__(self, config):
        self.create_external_users = config.get('create_external_users', False)
        self.root_url = config.get('root_url', '')
        self.jwt_token = config.get('jwt_token', '')
        self.grant_number = config.get('grant_number', '')
        self.resource_name = config.get('resource_name', '')

    def has_permission(self, internal_user):
        external_user_mapping = self.get_external_user_mapping({
            "user_id": internal_user,
            "user_authentication_name": type(self).__name__
        })

        if not external_user_mapping:
            # If the mapping does not exist, we have to try to "find" an external use
            # based on the info we have from the internal user
            external_user = self.get_external_user({"username": internal_user.username})

            if not external_user:
                # No user found, return false
                if self.create_external_users:
                    external_user = self.create_external_user(model_to_dict(internal_user))
                    if not external_user:
                        return False
                else:
                    return False

            # User found, create mapping
            external_user_mapping = self.create_external_user_mapping({
                'user_id': internal_user,
                'user_authentication_name': type(self).__name__,
                'external_user_id': external_user['external_user_id'],
                'external_username': external_user['external_username'],
                'external_user_details': external_user['external_user_details']
            })
            # If the mapping does exist, we just get that external user, to confirm it exists
            return external_user_mapping \
                if self.get_external_user({'external_user_id': external_user_mapping.external_user_id}) \
                else False
        else:
            return external_user_mapping

    def api_authenticate(self, request):
        body = json.loads(request.body)

        if 'client_token' not in body:
            raise ParseError('Missing client_token. Please have admin generate a token for you.')

        if 'user_info' not in body:
            raise ParseError('Missing user_info. Please provide user_info to get user_token.')

        try:
            client_token = body['client_token']
            token = Token.objects.get(key=client_token)
            token_user = token.user

            if not token_user.groups.filter(name='api_clients').exists():
                raise PermissionDenied('Token is invalid for api_authentication. '
                                       'Please contact administrator to generate valid token.')

            # Let's require username and email here
            user_info = body['user_info']

            if 'username' not in user_info or 'email' not in user_info:
                raise ParseError('Missing username or email in user_info.')

            external_user_mapping = self.get_external_user_mapping({
                'user_authentication_name': type(self).__name__,
                'external_username': user_info['username']
            })

            if not external_user_mapping:
                internal_user = self.get_internal_user({
                    'username': user_info['username'],
                    'email': user_info['email']
                })

                if not internal_user:
                    internal_user = self.create_internal_user({
                        "first_name": user_info.get('first_name', ''),
                        "last_name": user_info.get('last_name', ''),
                        "username": user_info['username'],
                        "email": user_info['email']
                    })

                return internal_user
            else:
                return external_user_mapping.user_id

        except Exception as e:
            # TODO: Move print to log
            print(e)
            return e

    def create_external_user(self, user_info):
        allocation = self.get_allocation()

        body = {
            "operationName": "AddUserWithAllocations",
            "query": """
                    mutation AddUserWithAllocations($input: AddUserInput!) {
                        addUser(input: $input) {   
                            user {
                                uid
                                pscId
                                username
                                name {
                                    first
                                    last
                                }
                                allocationUsers {
                                    startDate
                                    endDate
                                    active
                                    user {
                                        name {
                                           first
                                            last
                                        }
                                    }
                                    allocation {
                                        gid
                                        grant {
                                            number
                                        }
                                    }
                                }
                            }
                        }
                    }
            """,
            "variables": {
                "input": {
                    "name": {
                        "first": f"{user_info.get('first_name', user_info.get('username'))}",
                        "last": f"{user_info.get('last_name', user_info.get('username'))}"
                    },
                    "affiliation": {
                        "affiliationCode": "WS_API"
                    },
                    "email": {
                        "primary": f"{user_info['email']}"
                    },
                    "allocations": [
                        {
                            "allocationId": f"{allocation.get('id')}"
                        }
                    ]
                }
            }

        }

        response = http_r.post(self.root_url, json=body, headers={'Authorization': f"JWT {self.jwt_token}"})
        external_user = response.json()

        if 'errors' in external_user:
            message = ''
            for error in external_user.get('errors', {}):
                message += f"{error.get('message', '')}\n"
            raise PermissionDenied(f'Issue(s) when creating user:\n {message}')

        external_user = external_user.get('data', {}).get('addUser', {}).get('user', {})
        gid = False

        if external_user is None:
            return external_user

        for allocation in external_user.get('allocationUsers', []):
            if allocation.get('grant', {}).get('number', False) == self.grant_number:
                gid = allocation.get('gid', False)

        return {
            'external_user_id': external_user['pscId'],
            'external_username': external_user['username'],
            'external_user_uid': external_user['uid'],
            'external_user_gid': gid,
            'external_user_details': external_user
        } if external_user else external_user

    def get_external_user(self, external_user_info):
        variables = {"username": external_user_info['username']} if 'username' in external_user_info \
            else {"pscId": external_user_info['external_user_id']}

        body = {
            "operationName": "GetUserAndAllocationUsers",
            "query": """
                    query GetUserAndAllocationUsers($pscId: String, $username: String) {
                        user(pscId: $pscId, username: $username) {
                            uid
                            pscId
                            username
                            name {
                              first
                              last
                            }
                            email {
                              primary
                            }
                            allocationUsers {
                              allocation {
                                gid
                                grant {
                                  number
                                }
                                resource {
                                  name
                                }
                                startDate
                                endDate
                                active
                              }
                            }
                        }
                    }
            """,
            "variables": variables

        }

        response = http_r.post(self.root_url, json=body,
                               headers={'Authorization': f'JWT {self.jwt_token}'})
        external_user = response.json().get('data', {}).get('user', {})

        if external_user is None:
            return external_user

        gid = False

        for allocation_user in external_user.get('allocationUsers', []):
            allocation = allocation_user.get('allocation', {})
            if allocation.get('grant', {}).get('number', False) == self.grant_number:
                gid = allocation.get('gid', False)

        if not gid:
            # TODO: If this user is not assigned to this grant, then we need to assign the user
            pass

        return {
            'external_user_id': external_user['pscId'],
            'external_username': external_user['username'],
            'external_user_uid': external_user['uid'],
            'external_user_gid': gid,
            'external_user_details': external_user
        } if external_user else external_user

    def delete_external_user(self, user_id):
        pass

    def get_allocation(self):
        body = {
            "operationName": "",
            "query": """
                query GetAllocationAndAllocationUsers($grantNumber: String!, $resourceName: String!) {
                    allocation(grantNumber: $grantNumber, resourceName: $resourceName) {
                    id
                    startDate
                    endDate
                    active
                    grant {
                      number
                    }
                    resource {
                      name
                      active
                    }
                    allocationUsers {
                      active
                      user {
                        name {
                          first
                          last
                        }
                      }
                    }
                  }
                }
            """,
            "variables": {
              "grantNumber": f"{self.grant_number}",
              "resourceName": f"{self.resource_name}"
            }
        }

        response = http_r.post(self.root_url, json=body,
                               headers={'Authorization': f'JWT {self.jwt_token}'})
        allocation = response.json().get('data', {}).get('allocation', None)

        if not allocation:
            raise PermissionDenied(f'No valid allocation found for grant number {self.grant_number} and resource {self.resource_name}')

        return allocation

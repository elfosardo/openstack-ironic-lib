# Copyright 2020 Red Hat, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import base64
import binascii

import bcrypt
from oslo_log import log

from ironic_lib.common.i18n import _
from ironic_lib import exception

LOG = log.getLogger(__name__)


class BasicAuthMiddleware(object):
    """Middleware which performs HTTP basic authentication on requests

    """
    def __init__(self, app, auth_file):
        self.app = app
        self.auth_file = auth_file
        validate_auth_file(auth_file)

    def __call__(self, env, start_response):

        try:
            token = parse_header(env)
            username, password = parse_token(token)
            env.update(authenticate(self.auth_file, username, password))

            return self.app(env, start_response)

        except exception.IronicException as e:
            status = '%s %s' % (int(e.code), str(e))
            headers = [(k, v) for k, v in e.headers.items()]
            start_response(status, headers)


def authenticate(auth_file, username, password):
    """Finds username and password match in Apache style user auth file

    The user auth file format is expected to comply with Apache
    documentation[1] however the bcrypt password digest is the *only*
    digest format supported.

    [1] https://httpd.apache.org/docs/current/misc/password_encryptions.html

    :param: auth_file: Path to user auth file
    :param: username: Username to authenticate
    :param: password: Password encoded as bytes
    :returns: A dictionary of WSGI environment values to append to the request
    :raises: Unauthorized, if no file entries match supplied username/password
    """
    line_prefix = username + ':'
    try:
        with open(auth_file, 'r') as f:
            for line in f:
                entry = line.strip()
                if entry and entry.startswith(line_prefix):
                    return auth_entry(entry, password)
    except OSError:
        raise exception.ConfigInvalid(
            error_msg=_('Problem reading auth user file'))
    # reached end of file with no matches
    unauthorized()


def auth_entry(entry, password):
    """Compare a password with a single user auth file entry

    :param: entry: Line from auth user file to use for authentication
    :param: password: Password encoded as bytes
    :returns: A dictionary of WSGI environment values to append to the request
    :raises: Unauthorized, if the entry doesn't match supplied password or
        if the entry is crypted with a method other than bcrypt
    """
    username, crypted = parse_entry(entry)

    if not bcrypt.checkpw(password, crypted):
        unauthorized()

    return {
        'HTTP_X_USER': username,
        'HTTP_X_USER_NAME': username
    }


def validate_auth_file(auth_file):
    """Read the auth user file and validate its correctness

    :param: auth_file: Path to user auth file
    :raises: ConfigInvalid on validation error
    """
    try:
        with open(auth_file, 'r') as f:
            for line in f:
                entry = line.strip()
                if entry and ':' in entry:
                    parse_entry(entry)
    except OSError:
        raise exception.ConfigInvalid(
            error_msg=_('Problem reading auth user file: %s') % auth_file)


def parse_entry(entry):
    """Extrace the username and crypted password from a user auth file entry

    :param: entry: Line from auth user file to use for authentication
    :returns: a tuple of username and crypted password
    :raises: ConfigInvalid if the password is not in the supported bcrypt
             format
    """
    username, crypted_str = entry.split(':', maxsplit=1)
    crypted = crypted_str.encode('utf-8')

    if not crypted.startswith(b'$2y$'):
        error_msg = _('Only bcrypt digested passwords are supported for '
                      '%(username)s') % {'username': username}
        raise exception.ConfigInvalid(error_msg=error_msg)
    return username, crypted


def parse_token(token):
    """Parse the token portion of the Authentication header value

    :param: token: Token value from basic authorization header
    :returns: tuple of username, password
    :raises: Unauthorized, if username and password could not be parsed for any
        reason
    """
    try:
        if isinstance(token, str):
            token = token.encode('utf-8')
        auth_pair = base64.b64decode(token, validate=True)
        (username, password) = auth_pair.split(b':', maxsplit=1)

        return (username.decode('utf-8'), password)
    except (TypeError, binascii.Error, ValueError):
        raise exception.BadRequest(_('Could not decode authorization token'))


def parse_header(env):
    """Parse WSGI environment for Authorization header of type Basic

    :param: env: WSGI environment to get header from
    :returns: Token portion of the header value
    :raises: Unauthorized, if header is missing or if the type is not Basic
    """
    try:
        auth_header = env.pop('HTTP_AUTHORIZATION')
    except KeyError:
        unauthorized(_('Authorization required'))
    try:
        auth_type, token = auth_header.strip().split(maxsplit=1)
    except (ValueError, AttributeError):
        raise exception.BadRequest(_('Could not parse Authorization header'))

    if auth_type.lower() != 'basic':
        raise exception.BadRequest(_('Unsupported authorization type: '
                                   '%(auth_type)s') % {'auth_type': auth_type})
    return token


def unauthorized(message=None):
    """Raise an Unauthorized exception to prompt for basic authentication

    :param: message: Optional message for esception
    :raises: Unauthorized with WWW-Authenticate header set
    """
    if not message:
        message = _('Incorrect username or password')
    e = exception.Unauthorized(message)
    e.headers['WWW-Authenticate'] = 'Basic realm="Baremetal API"'
    raise e
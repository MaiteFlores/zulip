from django.contrib.auth.models import User
from django.test import TestCase
from django.test.simple import DjangoTestSuiteRunner
from django.utils.timezone import now
from django.db.models import Q

from zephyr.models import Message, UserProfile, Stream, Recipient, Subscription, \
    filter_by_subscriptions, get_display_recipient, Realm, Client
from zephyr.tornadoviews import json_get_updates, api_get_messages
from zephyr.views import gather_subscriptions, api_get_profile, \
    api_get_public_streams, api_add_subscriptions, api_get_subscribers
from zephyr.decorator import RespondAsynchronously, RequestVariableConversionError
from zephyr.lib.initial_password import initial_password, initial_api_key
from zephyr.lib.actions import do_send_message
from zephyr.lib.bugdown import convert

import simplejson
import subprocess
import optparse
from django.conf import settings
import re
import sys

try:
    settings.TEST_SUITE
except:
    print
    print "ERROR: Test suite only runs correctly with --settings=humbug.test_settings"
    print
    sys.exit(1)

def find_key_by_email(address):
    from django.core.mail import outbox
    key_regex = re.compile("accounts/do_confirm/([a-f0-9]{40})>")
    for message in reversed(outbox):
        if address in message.to:
            return key_regex.search(message.body).groups()[0]

def message_ids(result):
    return set(message['id'] for message in result['messages'])

class AuthedTestCase(TestCase):
    def login(self, email, password=None):
        if password is None:
            password = initial_password(email)
        return self.client.post('/accounts/login/',
                                {'username':email, 'password':password})

    def register(self, username, password):
        self.client.post('/accounts/home/',
                         {'email': username + '@humbughq.com'})
        return self.submit_reg_form_for_user(username, password)

    def submit_reg_form_for_user(self, username, password):
        """
        Stage two of the two-step registration process.

        If things are working correctly the account should be fully
        registered after this call.
        """
        return self.client.post('/accounts/register/',
                                {'full_name': username, 'password': password,
                                 'key': find_key_by_email(username + '@humbughq.com'),
                                 'terms': True})

    def get_api_key(self, email):
        return initial_api_key(email)

    def get_user_profile(self, email):
        """
        Given an email address, return the UserProfile object for the
        User that has that email.
        """
        # Usernames are unique, even across Realms.
        return UserProfile.objects.get(user__email=email)

    def send_message(self, sender_name, recipient_name, message_type):
        sender = self.get_user_profile(sender_name)
        if message_type == Recipient.PERSONAL:
            recipient = self.get_user_profile(recipient_name)
        else:
            recipient = Stream.objects.get(name=recipient_name, realm=sender.realm)
        recipient = Recipient.objects.get(type_id=recipient.id, type=message_type)
        pub_date = now()
        (sending_client, _) = Client.objects.get_or_create(name="test suite")
        do_send_message(Message(sender=sender, recipient=recipient, subject="test",
                                pub_date=pub_date, sending_client=sending_client))

    def users_subscribed_to_stream(self, stream_name, realm_domain):
        realm = Realm.objects.get(domain=realm_domain)
        stream = Stream.objects.get(name=stream_name, realm=realm)
        recipient = Recipient.objects.get(type_id=stream.id, type=Recipient.STREAM)
        subscriptions = Subscription.objects.filter(recipient=recipient)

        return [subscription.user_profile.user for subscription in subscriptions]

    def message_stream(self, user):
        return filter_by_subscriptions(Message.objects.all(), user)

    def assert_json_success(self, result):
        """
        Successful POSTs return a 200 and JSON of the form {"result": "success",
        "msg": ""}.
        """
        self.assertEquals(result.status_code, 200)
        json = simplejson.loads(result.content)
        self.assertEquals(json.get("result"), "success")
        # We have a msg key for consistency with errors, but it typically has an
        # empty value.
        self.assertIn("msg", json)

    def get_json_error(self, result):
        self.assertEquals(result.status_code, 400)
        json = simplejson.loads(result.content)
        self.assertEquals(json.get("result"), "error")
        return json['msg']

    def assert_json_error(self, result, msg):
        """
        Invalid POSTs return a 400 and JSON of the form {"result": "error",
        "msg": "reason"}.
        """
        self.assertEquals(self.get_json_error(result), msg)

    def assert_json_error_contains(self, result, msg_substring):
        self.assertIn(msg_substring, self.get_json_error(result))

class PublicURLTest(TestCase):
    """
    Account creation URLs are accessible even when not logged in. Authenticated
    URLs redirect to a page.
    """
    fixtures = ['messages.json']

    def fetch(self, method, urls, expected_status):
        for url in urls:
            if method == "get":
                response = self.client.get(url)
            else:
                response = self.client.post(url)
            self.assertEqual(response.status_code, expected_status,
                             msg="Expected %d, received %d for %s to %s" % (
                    expected_status, response.status_code, method, url))

    def test_public_urls(self):
        """
        Test which views are accessible when not logged in.
        """
        # FIXME: We should also test the Tornado URLs -- this codepath
        # can't do so because this Django test mechanism doesn't go
        # through Tornado.
        get_urls = {200: ["/accounts/home/", "/accounts/login/"],
                    302: ["/"],
                }
        post_urls = {200: ["/accounts/login/"],
                     302: ["/accounts/logout/"],
                     401: ["/json/get_public_streams",
                           "/json/get_old_messages",
                           "/json/update_pointer",
                           "/json/send_message",
                           "/json/invite_users",
                           "/json/settings/change",
                           "/json/subscriptions/list",
                           "/json/subscriptions/remove",
                           "/json/subscriptions/exists",
                           "/json/subscriptions/add",
                           "/json/subscriptions/property",
                           "/json/get_subscribers",
                           "/json/fetch_api_key",
                           ],
                     400: ["/api/v1/get_profile",
                           "/api/v1/get_old_messages",
                           "/api/v1/get_public_streams",
                           "/api/v1/subscriptions/list",
                           "/api/v1/subscriptions/add",
                           "/api/v1/subscriptions/remove",
                           "/api/v1/get_subscribers",
                           "/api/v1/send_message",
                           "/api/v1/update_pointer",
                           "/api/v1/external/github",
                           "/api/v1/fetch_api_key",
                           ],
                }
        for status_code, url_set in get_urls.iteritems():
            self.fetch("get", url_set, status_code)
        for status_code, url_set in post_urls.iteritems():
            self.fetch("post", url_set, status_code)

class LoginTest(AuthedTestCase):
    """
    Logging in, registration, and logging out.
    """
    fixtures = ['messages.json']

    def test_login(self):
        self.login("hamlet@humbughq.com")
        user = User.objects.get(email='hamlet@humbughq.com')
        self.assertEqual(self.client.session['_auth_user_id'], user.id)

    def test_login_bad_password(self):
        self.login("hamlet@humbughq.com", "wrongpassword")
        self.assertIsNone(self.client.session.get('_auth_user_id', None))

    def test_register(self):
        self.register("test", "test")
        user = User.objects.get(email='test@humbughq.com')
        self.assertEqual(self.client.session['_auth_user_id'], user.id)

    def test_logout(self):
        self.login("hamlet@humbughq.com")
        self.client.post('/accounts/logout/')
        self.assertIsNone(self.client.session.get('_auth_user_id', None))


class PersonalMessagesTest(AuthedTestCase):
    fixtures = ['messages.json']

    def test_auto_subbed_to_personals(self):
        """
        Newly created users are auto-subbed to the ability to receive
        personals.
        """
        self.register("test", "test")
        user = User.objects.get(email='test@humbughq.com')
        old_messages = self.message_stream(user)
        self.send_message("test@humbughq.com", "test@humbughq.com", Recipient.PERSONAL)
        new_messages = self.message_stream(user)
        self.assertEqual(len(new_messages) - len(old_messages), 1)

        recipient = Recipient.objects.get(type_id=user.id, type=Recipient.PERSONAL)
        self.assertEqual(new_messages[-1].recipient, recipient)

    def test_personal_to_self(self):
        """
        If you send a personal to yourself, only you see it.
        """
        old_users = list(User.objects.all())
        self.register("test1", "test1")

        old_messages = []
        for user in old_users:
            old_messages.append(len(self.message_stream(user)))

        self.send_message("test1@humbughq.com", "test1@humbughq.com", Recipient.PERSONAL)

        new_messages = []
        for user in old_users:
            new_messages.append(len(self.message_stream(user)))

        self.assertEqual(old_messages, new_messages)

        user = User.objects.get(email="test1@humbughq.com")
        recipient = Recipient.objects.get(type_id=user.id, type=Recipient.PERSONAL)
        self.assertEqual(self.message_stream(user)[-1].recipient, recipient)

    def test_personal(self):
        """
        If you send a personal, only you and the recipient see it.
        """
        self.login("hamlet@humbughq.com")

        old_sender = User.objects.filter(email="hamlet@humbughq.com")
        old_sender_messages = len(self.message_stream(old_sender))

        old_recipient = User.objects.filter(email="othello@humbughq.com")
        old_recipient_messages = len(self.message_stream(old_recipient))

        other_users = User.objects.filter(~Q(email="hamlet@humbughq.com") & ~Q(email="othello@humbughq.com"))
        old_other_messages = []
        for user in other_users:
            old_other_messages.append(len(self.message_stream(user)))

        self.send_message("hamlet@humbughq.com", "othello@humbughq.com", Recipient.PERSONAL)

        # Users outside the conversation don't get the message.
        new_other_messages = []
        for user in other_users:
            new_other_messages.append(len(self.message_stream(user)))

        self.assertEqual(old_other_messages, new_other_messages)

        # The personal message is in the streams of both the sender and receiver.
        self.assertEqual(len(self.message_stream(old_sender)),
                         old_sender_messages + 1)
        self.assertEqual(len(self.message_stream(old_recipient)),
                         old_recipient_messages + 1)

        sender = User.objects.get(email="hamlet@humbughq.com")
        receiver = User.objects.get(email="othello@humbughq.com")
        recipient = Recipient.objects.get(type_id=receiver.id, type=Recipient.PERSONAL)
        self.assertEqual(self.message_stream(sender)[-1].recipient, recipient)
        self.assertEqual(self.message_stream(receiver)[-1].recipient, recipient)

class StreamMessagesTest(AuthedTestCase):
    fixtures = ['messages.json']

    def test_message_to_stream(self):
        """
        If you send a message to a stream, everyone subscribed to the stream
        receives the messages.
        """
        subscribers = self.users_subscribed_to_stream("Scotland", "humbughq.com")
        old_subscriber_messages = []
        for subscriber in subscribers:
            old_subscriber_messages.append(len(self.message_stream(subscriber)))

        non_subscribers = [user for user in User.objects.all() if user not in subscribers]
        old_non_subscriber_messages = []
        for non_subscriber in non_subscribers:
            old_non_subscriber_messages.append(len(self.message_stream(non_subscriber)))

        a_subscriber_email = subscribers[0].email
        self.login(a_subscriber_email)
        self.send_message(a_subscriber_email, "Scotland", Recipient.STREAM)

        new_subscriber_messages = []
        for subscriber in subscribers:
           new_subscriber_messages.append(len(self.message_stream(subscriber)))

        new_non_subscriber_messages = []
        for non_subscriber in non_subscribers:
            new_non_subscriber_messages.append(len(self.message_stream(non_subscriber)))

        self.assertEqual(old_non_subscriber_messages, new_non_subscriber_messages)
        self.assertEqual(new_subscriber_messages, [elt + 1 for elt in old_subscriber_messages])

class PointerTest(AuthedTestCase):
    fixtures = ['messages.json']

    def test_update_pointer(self):
        """
        Posting a pointer to /update (in the form {"pointer": pointer}) changes
        the pointer we store for your UserProfile.
        """
        self.login("hamlet@humbughq.com")
        self.assertEquals(self.get_user_profile("hamlet@humbughq.com").pointer, -1)
        result = self.client.post("/json/update_pointer", {"pointer": 1})
        self.assert_json_success(result)
        self.assertEquals(self.get_user_profile("hamlet@humbughq.com").pointer, 1)

    def test_api_update_pointer(self):
        """
        Same as above, but for the API view
        """
        email = "hamlet@humbughq.com"
        api_key = self.get_api_key(email)
        self.assertEquals(self.get_user_profile(email).pointer, -1)
        result = self.client.post("/api/v1/update_pointer", {"email": email,
                                                             "api-key": api_key,
                                                             "client_id": "blah",
                                                             "pointer": 1})
        self.assert_json_success(result)
        self.assertEquals(self.get_user_profile(email).pointer, 1)

    def test_missing_pointer(self):
        """
        Posting json to /json/update_pointer which does not contain a pointer key/value pair
        returns a 400 and error message.
        """
        self.login("hamlet@humbughq.com")
        self.assertEquals(self.get_user_profile("hamlet@humbughq.com").pointer, -1)
        result = self.client.post("/json/update_pointer", {"foo": 1})
        self.assert_json_error(result, "Missing 'pointer' argument")
        self.assertEquals(self.get_user_profile("hamlet@humbughq.com").pointer, -1)

    def test_invalid_pointer(self):
        """
        Posting json to /json/update_pointer with an invalid pointer returns a 400 and error
        message.
        """
        self.login("hamlet@humbughq.com")
        self.assertEquals(self.get_user_profile("hamlet@humbughq.com").pointer, -1)
        result = self.client.post("/json/update_pointer", {"pointer": "foo"})
        self.assert_json_error(result, "Bad value for 'pointer': foo")
        self.assertEquals(self.get_user_profile("hamlet@humbughq.com").pointer, -1)

    def test_pointer_out_of_range(self):
        """
        Posting json to /json/update_pointer with an out of range (< 0) pointer returns a 400
        and error message.
        """
        self.login("hamlet@humbughq.com")
        self.assertEquals(self.get_user_profile("hamlet@humbughq.com").pointer, -1)
        result = self.client.post("/json/update_pointer", {"pointer": -2})
        self.assert_json_error(result, "Bad value for 'pointer': -2")
        self.assertEquals(self.get_user_profile("hamlet@humbughq.com").pointer, -1)

class MessagePOSTTest(AuthedTestCase):
    fixtures = ['messages.json']

    def test_message_to_self(self):
        """
        Sending a message to a stream to which you are subscribed is
        successful.
        """
        self.login("hamlet@humbughq.com")
        result = self.client.post("/json/send_message", {"type": "stream",
                                                         "to": "Verona",
                                                         "client": "test suite",
                                                         "content": "Test message",
                                                         "subject": "Test subject"})
        self.assert_json_success(result)

    def test_api_message_to_self(self):
        """
        Same as above, but for the API view
        """
        email = "hamlet@humbughq.com"
        api_key = self.get_api_key(email)
        result = self.client.post("/api/v1/send_message", {"type": "stream",
                                                           "to": "Verona",
                                                           "client": "test suite",
                                                           "content": "Test message",
                                                           "subject": "Test subject",
                                                           "email": email,
                                                           "api-key": api_key})
        self.assert_json_success(result)

    def test_message_to_nonexistent_stream(self):
        """
        Sending a message to a nonexistent stream fails.
        """
        self.login("hamlet@humbughq.com")
        self.assertFalse(Stream.objects.filter(name="nonexistent_stream"))
        result = self.client.post("/json/send_message", {"type": "stream",
                                                         "to": "nonexistent_stream",
                                                         "client": "test suite",
                                                         "content": "Test message",
                                                         "subject": "Test subject"})
        self.assert_json_error(result, "Stream does not exist")

    def test_personal_message(self):
        """
        Sending a personal message to a valid username is successful.
        """
        self.login("hamlet@humbughq.com")
        result = self.client.post("/json/send_message", {"type": "private",
                                                         "content": "Test message",
                                                         "client": "test suite",
                                                         "to": "othello@humbughq.com"})
        self.assert_json_success(result)

    def test_personal_message_to_nonexistent_user(self):
        """
        Sending a personal message to an invalid email returns error JSON.
        """
        self.login("hamlet@humbughq.com")
        result = self.client.post("/json/send_message", {"type": "private",
                                                         "content": "Test message",
                                                         "client": "test suite",
                                                         "to": "nonexistent"})
        self.assert_json_error(result, "Invalid email 'nonexistent'")

    def test_invalid_type(self):
        """
        Sending a message of unknown type returns error JSON.
        """
        self.login("hamlet@humbughq.com")
        result = self.client.post("/json/send_message", {"type": "invalid type",
                                                         "content": "Test message",
                                                         "client": "test suite",
                                                         "to": "othello@humbughq.com"})
        self.assert_json_error(result, "Invalid message type")

    def test_mirrored_huddle(self):
        """
        Sending a mirrored huddle message works
        """
        self.login("starnine@mit.edu")
        result = self.client.post("/json/send_message", {"type": "private",
                                                         "sender": "sipbtest@mit.edu",
                                                         "content": "Test message",
                                                         "client": "zephyr_mirror",
                                                         "to": simplejson.dumps(["starnine@mit.edu",
                                                                                 "espuser@mit.edu"])})
        self.assert_json_success(result)

    def test_mirrored_personal(self):
        """
        Sending a mirrored personal message works
        """
        self.login("starnine@mit.edu")
        result = self.client.post("/json/send_message", {"type": "private",
                                                         "sender": "sipbtest@mit.edu",
                                                         "content": "Test message",
                                                         "client": "zephyr_mirror",
                                                         "to": "starnine@mit.edu"})
        self.assert_json_success(result)

class SubscriptionPropertiesTest(AuthedTestCase):
    fixtures = ['messages.json']

    def test_get_stream_colors(self):
        """
        A GET request to
        /json/subscriptions/property?property=stream_colors returns a
        list of (stream, color) pairs, both of which are strings.
        """
        test_email = "hamlet@humbughq.com"
        self.login(test_email)
        result = self.client.get("/json/subscriptions/property",
                                  {"property": "stream_colors"})

        self.assert_json_success(result)
        json = simplejson.loads(result.content)
        self.assertIn("stream_colors", json)

        subs = gather_subscriptions(self.get_user_profile(test_email))
        for stream, color in json["stream_colors"]:
            self.assertIsInstance(color,  str)
            self.assertIsInstance(stream, str)
            self.assertIn((stream, color), subs)
            subs.remove((stream, color))
        self.assertFalse(subs)

    def test_set_stream_color(self):
        """
        A POST request to /json/subscriptions/property with stream_name and
        color data sets the stream color, and for that stream only.
        """
        test_email = "hamlet@humbughq.com"
        self.login(test_email)

        old_subs = gather_subscriptions(self.get_user_profile(test_email))
        stream_name, old_color = old_subs[0]
        new_color = "#ffffff" # TODO: ensure that this is different from old_color
        result = self.client.post("/json/subscriptions/property",
                                  {"property": "stream_colors",
                                   "stream_name": stream_name,
                                   "color": "#ffffff"})

        self.assert_json_success(result)

        new_subs = gather_subscriptions(self.get_user_profile(test_email))
        self.assertIn((stream_name, new_color), new_subs)

        old_subs.remove((stream_name, old_color))
        new_subs.remove((stream_name, new_color))
        self.assertEqual(old_subs, new_subs)

    def test_set_color_missing_stream_name(self):
        """
        Updating the stream_colors property requires a stream_name.
        """
        test_email = "hamlet@humbughq.com"
        self.login(test_email)
        result = self.client.post("/json/subscriptions/property",
                                  {"property": "stream_colors",
                                   "color": "#ffffff"})

        self.assert_json_error(result, "Missing stream_name")

    def test_set_color_missing_color(self):
        """
        Updating the stream_colors property requires a color.
        """
        test_email = "hamlet@humbughq.com"
        self.login(test_email)
        result = self.client.post("/json/subscriptions/property",
                                  {"property": "stream_colors",
                                   "stream_name": "test"})

        self.assert_json_error(result, "Missing color")

    def test_set_invalid_property(self):
        """
        Trying to set an invalid property returns a JSON error.
        """
        self.login("hamlet@humbughq.com")
        result = self.client.post("/json/subscriptions/property",
                                  {"property": "bad"})

        self.assert_json_error(result,
                               "Unknown property or invalid verb for bad")

class GetOldMessagesTest(AuthedTestCase):
    fixtures = ['messages.json']

    def post_with_params(self, modified_params):
        post_params = {"anchor": 1, "num_before": 1, "num_after": 1}
        post_params.update(modified_params)
        result = self.client.post("/json/get_old_messages", dict(post_params))
        self.assert_json_success(result)
        return simplejson.loads(result.content)

    def check_well_formed_messages_response(self, result):
        self.assertIn("messages", result)
        self.assertIsInstance(result["messages"], list)
        for message in result["messages"]:
            for field in ("content", "content_type", "display_recipient",
                          "gravatar_hash", "recipient_id", "sender_full_name",
                          "sender_short_name", "timestamp"):
                self.assertIn(field, message)

    def test_successful_get_old_messages(self):
        """
        A call to /json/get_old_messages with valid parameters returns a list of
        messages.
        """
        self.login("hamlet@humbughq.com")
        self.check_well_formed_messages_response(self.post_with_params({}))

    def test_get_old_messages_with_narrow_pm_with(self):
        """
        A request for old messages with a narrow by pm-with only returns
        conversations with that user.
        """
        me = 'hamlet@humbughq.com'
        def dr_emails(dr):
            return ','.join(sorted(set([r['email'] for r in dr] + [me])))

        personals = [m for m in self.message_stream(User.objects.get(email=me))
            if m.recipient.type == Recipient.PERSONAL
            or m.recipient.type == Recipient.HUDDLE]
        if not personals:
            # FIXME: This is bad.  We should use test data that is guaranteed
            # to contain some personals for every user.  See #617.
            return
        emails = dr_emails(get_display_recipient(personals[0].recipient))

        self.login(me)
        result = self.post_with_params({"narrow": simplejson.dumps(
                    [['pm-with', emails]])})
        self.check_well_formed_messages_response(result)

        for message in result["messages"]:
            self.assertEquals(dr_emails(message['display_recipient']), emails)

    def test_get_old_messages_with_narrow_stream(self):
        """
        A request for old messages with a narrow by stream only returns
        messages for that stream.
        """
        self.login("hamlet@humbughq.com")
        # We need to send a message here to ensure that we actually
        # have a stream message in this narrow view.
        self.send_message("hamlet@humbughq.com", "Scotland", Recipient.STREAM)
        messages = self.message_stream(User.objects.get(email="hamlet@humbughq.com"))
        stream_messages = filter(lambda msg: msg.recipient.type == Recipient.STREAM,
                                 messages)
        stream_name = get_display_recipient(stream_messages[0].recipient)
        stream_id = stream_messages[0].recipient.id

        result = self.post_with_params({"narrow": simplejson.dumps(
                    [['stream', stream_name]])})
        self.check_well_formed_messages_response(result)

        for message in result["messages"]:
            self.assertEquals(message["type"], "stream")
            self.assertEquals(message["recipient_id"], stream_id)

    def test_missing_params(self):
        """
        anchor, num_before, and num_after are all required
        POST parameters for get_old_messages.
        """
        self.login("hamlet@humbughq.com")

        required_args = (("anchor", 1), ("num_before", 1), ("num_after", 1))

        for i in range(len(required_args)):
            post_params = dict(required_args[:i] + required_args[i + 1:])
            result = self.client.post("/json/get_old_messages", post_params)
            self.assert_json_error(result,
                                   "Missing '%s' argument" % (required_args[i][0],))

    def test_bad_int_params(self):
        """
        anchor, num_before, num_after, and narrow must all be non-negative
        integers or strings that can be converted to non-negative integers.
        """
        self.login("hamlet@humbughq.com")

        other_params = [("narrow", {})]
        int_params = ["anchor", "num_before", "num_after"]

        bad_types = (False, "", "-1", -1)
        for idx, param in enumerate(int_params):
            for type in bad_types:
                # Rotate through every bad type for every integer
                # parameter, one at a time.
                post_params = dict(other_params + [(param, type)] + \
                                       [(other_param, 0) for other_param in \
                                            int_params[:idx] + int_params[idx + 1:]]
                                   )
                result = self.client.post("/json/get_old_messages", post_params)
                self.assert_json_error(result,
                                       "Bad value for '%s': %s" % (param, type))

    def test_bad_narrow_type(self):
        """
        narrow must be a list of string pairs.
        """
        self.login("hamlet@humbughq.com")

        other_params = [("anchor", 0), ("num_before", 0), ("num_after", 0)]

        bad_types = (False, 0, '', '{malformed json,',
            '{foo: 3}', '[1,2]', '[["x","y","z"]]')
        for type in bad_types:
            post_params = dict(other_params + [("narrow", type)])
            result = self.client.post("/json/get_old_messages", post_params)
            self.assert_json_error(result,
                                   "Bad value for 'narrow': %s" % (type,))

    def test_old_empty_narrow(self):
        """
        '{}' is accepted to mean 'no narrow', for use by old mobile clients.
        """
        self.login("hamlet@humbughq.com")
        all_result    = self.post_with_params({})
        narrow_result = self.post_with_params({'narrow': '{}'})

        for r in (all_result, narrow_result):
            self.check_well_formed_messages_response(r)

        self.assertEqual(message_ids(all_result), message_ids(narrow_result))

    def test_bad_narrow_operator(self):
        """
        Unrecognized narrow operators are rejected.
        """
        self.login("hamlet@humbughq.com")
        for operator in ['', 'foo', 'stream:verona', '__init__']:
            params = dict(anchor=0, num_before=0, num_after=0,
                narrow=simplejson.dumps([[operator, '']]))
            result = self.client.post("/json/get_old_messages", params)
            self.assert_json_error_contains(result,
                "Invalid narrow operator: unknown operator")

    def exercise_bad_narrow_operand(self, operator, operands, error_msg):
        other_params = [("anchor", 0), ("num_before", 0), ("num_after", 0)]
        for operand in operands:
            post_params = dict(other_params + [
                ("narrow", simplejson.dumps([[operator, operand]]))])
            result = self.client.post("/json/get_old_messages", post_params)
            self.assert_json_error_contains(result, error_msg)

    def test_bad_narrow_stream_content(self):
        """
        If an invalid stream name is requested in get_old_messages, an error is
        returned.
        """
        self.login("hamlet@humbughq.com")
        bad_stream_content = (0, [], ["x", "y"])
        self.exercise_bad_narrow_operand("stream", bad_stream_content,
            "Bad value for 'narrow'")

    def test_bad_narrow_one_on_one_email_content(self):
        """
        If an invalid 'pm-with' is requested in get_old_messages, an
        error is returned.
        """
        self.login("hamlet@humbughq.com")
        bad_stream_content = (0, [], ["x","y"])
        self.exercise_bad_narrow_operand("pm-with", bad_stream_content,
            "Bad value for 'narrow'")

    def test_bad_narrow_nonexistent_stream(self):
        self.login("hamlet@humbughq.com")
        self.exercise_bad_narrow_operand("stream", ['non-existent stream'],
            "Invalid narrow operator: unknown stream")

    def test_bad_narrow_nonexistent_email(self):
        self.login("hamlet@humbughq.com")
        self.exercise_bad_narrow_operand("pm-with", ['non-existent-user@humbughq.com'],
            "Invalid narrow operator: unknown user")


class InviteUserTest(AuthedTestCase):
    fixtures = ['messages.json']

    def invite(self, users, streams):
        """
        Invites the specified users to Humbug with the specified streams.

        users should be a string containing the users to invite, comma or
            newline separated.

        streams should be a list of strings.
        """

        return self.client.post("/json/invite_users",
                {"invitee_emails": users,
                    "stream": streams})

    def test_successful_invite_user(self):
        """
        A call to /json/invite_users with valid parameters causes an invitation
        email to be sent.
        """
        self.login("hamlet@humbughq.com")
        self.assert_json_success(self.invite("alice-test@humbughq.com", ["Denmark"]))
        self.assertTrue(find_key_by_email("alice-test@humbughq.com"))

    def test_multi_user_invite(self):
        """
        Invites multiple users with a variety of delimiters.
        """
        self.login("hamlet@humbughq.com")
        # Intentionally use a weird string.
        self.assert_json_success(self.invite(
"""bob-test@humbughq.com,     carol-test@humbughq.com,
dave-test@humbughq.com


earl-test@humbughq.com""", ["Denmark"]))
        for user in ("bob", "carol", "dave", "earl"):
            self.assertTrue(find_key_by_email("%s-test@humbughq.com" % user))

    def test_missing_params(self):
        """
        Tests inviting with various invalid parameters.
        """
        self.login("hamlet@humbughq.com")
        self.assert_json_error(
            self.client.post("/json/invite_users", {"invitee_emails": "foo@humbughq.com"}),
            "You must specify at least one stream for invitees to join.")

        for address in ("noatsign.com", "outsideyourdomain@example.net"):
            self.assert_json_error(
                self.invite(address, ["Denmark"]),
                "Some emails did not validate. No invites have been sent.")

    def test_invalid_stream(self):
        """
        Tests inviting to a non-existent stream.
        """
        self.login("hamlet@humbughq.com")
        self.assert_json_error(self.invite("iago-test@humbughq.com", ["NotARealStream"]),
                "Stream does not exist: NotARealStream. No invites were sent.")

class ChangeSettingsTest(AuthedTestCase):
    fixtures = ['messages.json']

    def post_with_params(self, modified_params):
        post_params = {"full_name": "Foo Bar",
                  "old_password": initial_password("hamlet@humbughq.com"),
                  "new_password": "foobar1", "confirm_password": "foobar1",
                  "enable_desktop_notifications": ""}
        post_params.update(modified_params)
        return self.client.post("/json/settings/change", dict(post_params))

    def check_well_formed_change_settings_response(self, result):
        self.assertIn("full_name", result)
        self.assertIn("enable_desktop_notifications", result)

    def test_successful_change_settings(self):
        """
        A call to /json/settings/change with valid parameters changes the user's
        settings correctly and returns correct values.
        """
        self.login("hamlet@humbughq.com")
        json_result = self.post_with_params({})
        self.assert_json_success(json_result)
        result = simplejson.loads(json_result.content)
        self.check_well_formed_change_settings_response(result)
        self.assertEquals(self.get_user_profile("hamlet@humbughq.com").
                full_name, "Foo Bar")
        self.assertEquals(self.get_user_profile("hamlet@humbughq.com").
                enable_desktop_notifications, False)
        self.client.post('/accounts/logout/')
        self.login("hamlet@humbughq.com", "foobar1")
        user = User.objects.get(email='hamlet@humbughq.com')
        self.assertEqual(self.client.session['_auth_user_id'], user.id)

    def test_missing_params(self):
        """
        full_name, old_password, and new_password are all required POST
        parameters for json_change_settings. (enable_desktop_notifications is
        false by default)
        """
        self.login("hamlet@humbughq.com")
        required_params = (("full_name", "Foo Bar"),
                  ("old_password", initial_password("hamlet@humbughq.com")),
                  ("new_password", initial_password("hamlet@humbughq.com")),
                  ("confirm_password", initial_password("hamlet@humbughq.com")))
        for i in range(len(required_params)):
            post_params = dict(required_params[:i] + required_params[i + 1:])
            result = self.client.post("/json/settings/change", post_params)
            self.assert_json_error(result,
                    "Missing '%s' argument" % (required_params[i][0],))

    def test_mismatching_passwords(self):
        """
        new_password and confirm_password must match
        """
        self.login("hamlet@humbughq.com")
        result = self.post_with_params({"new_password": "mismatched_password"})
        self.assert_json_error(result,
                "New password must match confirmation password!")

    def test_wrong_old_password(self):
        """
        new_password and confirm_password must match
        """
        self.login("hamlet@humbughq.com")
        result = self.post_with_params({"old_password": "bad_password"})
        self.assert_json_error(result, "Wrong password!")


class DummyHandler(object):
    def __init__(self, assert_callback):
        self.assert_callback = assert_callback

    # Mocks RequestHandler.async_callback, which wraps a callback to
    # handle exceptions.  We return the callback as-is.
    def async_callback(self, cb):
        return cb

    def write(self, response):
        raise NotImplemented

    def finish(self, response):
        if self.assert_callback:
            self.assert_callback(response)

class DummySession(object):
    session_key = "0"

class POSTRequestMock(object):
    method = "POST"

    def __init__(self, post_data, user, assert_callback=None):
        self.POST = post_data
        self.user = user
        self._tornado_handler = DummyHandler(assert_callback)
        self.session = DummySession()
        self.META = {'PATH_INFO': 'test'}

class GetUpdatesTest(AuthedTestCase):
    fixtures = ['messages.json']

    def common_test_get_updates(self, view_func, extra_post_data = {}):
        user = User.objects.get(email="hamlet@humbughq.com")

        def callback(response):
            correct_message_ids = [m.id for m in
                filter_by_subscriptions(Message.objects.all(), user)]
            for message in response['messages']:
                self.assertGreater(message['id'], 1)
                self.assertIn(message['id'], correct_message_ids)

        post_data = {}
        post_data.update(extra_post_data)
        request = POSTRequestMock(post_data, user, callback)
        self.assertEquals(view_func(request), RespondAsynchronously)

    def test_json_get_updates(self):
        """
        json_get_updates returns messages with IDs greater than the
        last_received ID.
        """
        self.login("hamlet@humbughq.com")
        self.common_test_get_updates(json_get_updates)

    def test_api_get_messages(self):
        """
        Same as above, but for the API view
        """
        email = "hamlet@humbughq.com"
        api_key = self.get_api_key(email)
        self.common_test_get_updates(api_get_messages, {'email': email, 'api-key': api_key})

    def test_missing_last_received(self):
        """
        Calling json_get_updates without any arguments should work
        """
        self.login("hamlet@humbughq.com")
        user = User.objects.get(email="hamlet@humbughq.com")

        request = POSTRequestMock({}, user)
        self.assertEquals(json_get_updates(request), RespondAsynchronously)

    def test_bad_input(self):
        """
        Specifying a bad value for 'pointer' should return an error
        """
        self.login("hamlet@humbughq.com")
        user = User.objects.get(email="hamlet@humbughq.com")

        request = POSTRequestMock({'pointer': 'foo'}, user)
        self.assertRaises(RequestVariableConversionError, json_get_updates, request)

class GetProfileTest(AuthedTestCase):
    fixtures = ['messages.json']

    def common_update_pointer(self, email, pointer):
        self.login(email)
        result = self.client.post("/json/update_pointer", {"pointer": 1})
        self.assert_json_success(result)

    def common_get_profile(self, email):
        user = User.objects.get(email=email)

        api_key = self.get_api_key(email)
        request = POSTRequestMock({'email': email, 'api-key': api_key}, user, None)
        result = api_get_profile(request)

        stream = self.message_stream(user)
        max_id = -1
        if len(stream) > 0:
            max_id = stream[-1].id

        self.assert_json_success(result)
        json = simplejson.loads(result.content)

        self.assertIn("client_id", json)
        self.assertIn("max_message_id", json)
        self.assertIn("pointer", json)

        self.assertEquals(json["max_message_id"], max_id)
        return json

    def test_api_get_empty_profile(self):
        """
        Ensure get_profile returns a max message id and returns successfully
        """
        json = self.common_get_profile("othello@humbughq.com")
        self.assertEquals(json["pointer"], -1)

    def test_profile_with_pointer(self):
        """
        Ensure get_profile returns a proper pointer id after the pointer is updated
        """
        json = self.common_get_profile("hamlet@humbughq.com")

        self.common_update_pointer("hamlet@humbughq.com", 1)
        json = self.common_get_profile("hamlet@humbughq.com")
        self.assertEquals(json["pointer"], 1)

        self.common_update_pointer("hamlet@humbughq.com", 0)
        json = self.common_get_profile("hamlet@humbughq.com")
        self.assertEquals(json["pointer"], 1)

class GetPublicStreamsTest(AuthedTestCase):
    fixtures = ['messages.json']

    def test_public_streams(self):
        """
        Ensure that get_public_streams successfully returns a list of streams
        """
        email = 'hamlet@humbughq.com'
        user = User.objects.get(email=email)

        api_key = self.get_api_key(email)
        request = POSTRequestMock({'email': email, 'api-key': api_key}, user, None)
        result = api_get_public_streams(request)

        self.assert_json_success(result)
        json = simplejson.loads(result.content)

        self.assertIn("streams", json)
        self.assertIsInstance(json["streams"], list)

class InviteOnlyStreamTest(AuthedTestCase):
    fixtures = ['messages.json']

    def common_subscribe_to_stream(self, email, streams, extra_post_data = {}, invite_only=False):
        user = User.objects.get(email=email)
        api_key = self.get_api_key(email)

        post_data = {'email': email,
                     'api-key': api_key,
                     'subscriptions': streams,
                     'invite_only': invite_only}
        post_data.update(extra_post_data)
        request = POSTRequestMock(post_data, user, None)
        result = api_add_subscriptions(request)
        return result

    def test_inviteonly(self):
        # Creating an invite-only stream is allowed
        email = 'hamlet@humbughq.com'
        result = self.common_subscribe_to_stream(email, '["Saxony"]', invite_only=True)
        self.assert_json_success(result)

        json = simplejson.loads(result.content)
        self.assertEquals(json["subscribed"], ['Saxony'])
        self.assertEquals(json["already_subscribed"], [])

        # Subscribing oneself to an invite-only stream is not allowed
        email = "othello@humbughq.com"
        result = self.common_subscribe_to_stream(email, '["Saxony"]')
        self.assert_json_error(result, "Unable to join an invite-only stream")

        # Inviting another user to an invite-only stream is allowed
        email = 'hamlet@humbughq.com'
        result = self.common_subscribe_to_stream(email, '["Saxony"]',
                                                 extra_post_data={'principal':
                                                                  'othello@humbughq.com'})
        self.assertEquals(json["subscribed"], ['Saxony'])
        self.assertEquals(json["already_subscribed"], [])

        # Make sure both users are subscribed to this stream
        user = User.objects.get(email=email)
        request = POSTRequestMock({'email':email,
                                   'api-key': self.get_api_key(email),
                                   'stream': 'Saxony'},
                                  user, None)
        result = api_get_subscribers(request)
        self.assert_json_success(result)
        json = simplejson.loads(result.content)

        self.assertTrue('othello@humbughq.com' in json['subscribers'])
        self.assertTrue('hamlet@humbughq.com' in json['subscribers'])

class BugdownTest(TestCase):

    def common_bugdown_test(self, text, expected):
        converted = convert(text)
        self.assertEquals(converted, expected)

    def test_codeblock_hilite(self):
        fenced_code = \
"""Hamlet said:
~~~~.python
def speak(self):
    x = 1
~~~~"""

        expected_convert = \
"""<p>Hamlet said:</p>
<div class="codehilite"><pre><span class="k">def</span> <span class="nf">\
speak</span><span class="p">(</span><span class="bp">self</span><span class="p">):</span>
    <span class="n">x</span> <span class="o">=</span> <span class="mi">1</span>
</pre></div>"""

        self.common_bugdown_test(fenced_code, expected_convert)

    def test_codeblock_multiline(self):
        fenced_code = \
"""Hamlet once said
~~~~
def func():
    x = 1


    y = 2

    z = 3
~~~~
And all was good."""

        expected_convert = \
"""<p>Hamlet once said</p>
<div class="codehilite"><pre>def func():
    x = 1

    y = 2

    z = 3
</pre></div>


<p>And all was good.</p>"""

        self.common_bugdown_test(fenced_code, expected_convert)


    def test_hanging_multi_codeblock(self):
        fenced_code = \
"""Hamlet said:
~~~~
def speak(self):
    x = 1
~~~~

Then he mentioned ````y = 4 + x**2```` and
~~~~
def foobar(self):
    return self.baz()"""

        expected_convert = \
"""<p>Hamlet said:</p>
<div class="codehilite"><pre>def speak(self):
    x = 1
</pre></div>


<p>Then he mentioned <code>y = 4 + x**2</code> and</p>
<div class="codehilite"><pre>def foobar(self):
    return self.baz()
</pre></div>"""
        self.common_bugdown_test(fenced_code, expected_convert)

    def test_dangerous_block(self):
        fenced_code = u'xxxxxx xxxxx xxxxxxxx xxxx. x xxxx xxxxxxxxxx:\n\n```\
"xxxx xxxx\\xxxxx\\xxxxxx"```\n\nxxx xxxx xxxxx:```xx.xxxxxxx(x\'^xxxx$\'\
, xx.xxxxxxxxx)```\n\nxxxxxxx\'x xxxx xxxxxxxxxx ```\'xxxx\'```, xxxxx \
xxxxxxxxx xxxxx ^ xxx $ xxxxxx xxxxx xxxxxxxxxxxx xxx xxxx xx x xxxx xx xxxx xx xxx xxxxx xxxxxx?'

        expected = """<p>xxxxxx xxxxx xxxxxxxx xxxx. x xxxx xxxxxxxxxx:</p>\n\
<p><code>"xxxx xxxx\\xxxxx\\xxxxxx"</code></p>\n<p>xxx xxxx xxxxx:<code>xx.xxxxxxx\
(x\'^xxxx$\', xx.xxxxxxxxx)</code></p>\n<p>xxxxxxx\'x xxxx xxxxxxxxxx <code>\'xxxx\'\
</code>, xxxxx xxxxxxxxx xxxxx ^ xxx $ xxxxxx xxxxx xxxxxxxxxxxx xxx xxxx xx x \
xxxx xx xxxx xx xxx xxxxx xxxxxx?</p>"""

        self.common_bugdown_test(fenced_code, expected)

        fenced_code = """``` one ```

``` two ```

~~~~
x = 1"""
        expected_convert = '<p><code>one</code></p>\n<p><code>two</code></p>\n<div class="codehilite"><pre>x = 1\n</pre></div>'
        self.common_bugdown_test(fenced_code, expected_convert)

class Runner(DjangoTestSuiteRunner):
    option_list = (
        optparse.make_option('--skip-generate',
            dest='generate', default=True, action='store_false',
            help='Skip generating test fixtures')
    ,)

    def __init__(self, generate, *args, **kwargs):
        if generate:
            subprocess.check_call("zephyr/tests/generate-fixtures");
        DjangoTestSuiteRunner.__init__(self, *args, **kwargs)

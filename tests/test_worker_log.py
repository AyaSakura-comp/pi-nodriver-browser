import unittest

import worker


class LoggableCommandTest(unittest.TestCase):
    # A `type` command can carry a piweb login detail (🔑) the planner only saw as a
    # name; the events log keeps the command without what it typed.
    def test_type_text_is_not_logged(self):
        self.assertEqual(worker.loggable('type e2 "pa$$ w0rd" --submit'), 'type e2 (text not logged) --submit')
        self.assertEqual(worker.loggable('type e2 "octo-user-7731"'), 'type e2 (text not logged)')

    def test_other_commands_are_logged_as_they_are(self):
        self.assertEqual(worker.loggable('open "https://example.com"'), 'open "https://example.com"')
        self.assertEqual(worker.loggable('click e3'), 'click e3')


if __name__ == '__main__':
    unittest.main()

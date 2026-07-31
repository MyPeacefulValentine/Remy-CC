"""Tests for PythonParser.extract_patterns (Django/PyQt/observer pattern extraction)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
from parsers.python_parser import PythonParser


@pytest.fixture
def parser():
    return PythonParser()


class TestDjangoSignals:
    def test_connect_detected(self, parser):
        source = '''
from django.db.models.signals import post_save

def setup():
    post_save.connect(handle_save)

def handle_save(sender, **kwargs):
    pass
'''
        patterns = parser.extract_patterns(source, "signals.py")
        connects = [p for p in patterns if p["pattern_type"] == "django_signal_connect"]
        assert len(connects) == 1
        assert connects[0]["signal_name"] == "post_save"
        assert connects[0]["handler"] == "handle_save"

    def test_send_detected(self, parser):
        source = '''
def notify():
    my_signal.send(sender=self.__class__)
'''
        patterns = parser.extract_patterns(source, "notify.py")
        sends = [p for p in patterns if p["pattern_type"] == "django_signal_send"]
        assert len(sends) == 1
        assert sends[0]["signal_name"] == "my_signal"
        assert sends[0]["handler"] == "notify"

    def test_no_signals_empty_result(self, parser):
        source = 'def plain():\n    return 1\n'
        patterns = parser.extract_patterns(source, "plain.py")
        assert patterns == []


class TestPyqtSignals:
    def test_connect_detected(self, parser):
        source = '''
from PyQt5.QtCore import pyqtSignal

class MyWidget:
    def setup(self):
        self.clicked.connect(self.on_click)

    def on_click(self):
        pass
'''
        patterns = parser.extract_patterns(source, "widget.py")
        connects = [p for p in patterns if p["pattern_type"] == "pyqt_signal_connect"]
        assert len(connects) == 1
        assert connects[0]["signal_name"] == "clicked"
        assert connects[0]["handler"] == "on_click"

    def test_emit_detected(self, parser):
        source = '''
from PySide2.QtCore import Signal

class Emitter:
    def fire(self):
        self.data_ready.emit(42)
'''
        patterns = parser.extract_patterns(source, "emitter.py")
        emits = [p for p in patterns if p["pattern_type"] == "pyqt_signal_emit"]
        assert len(emits) == 1
        assert emits[0]["signal_name"] == "data_ready"

    def test_no_pyqt_import_no_detection(self, parser):
        source = '''
class Fake:
    def setup(self):
        self.signal.connect(self.handler)
'''
        patterns = parser.extract_patterns(source, "fake.py")
        pyqt = [p for p in patterns if "pyqt" in p["pattern_type"]]
        assert len(pyqt) == 0


class TestObserverPattern:
    def test_register_detected(self, parser):
        source = '''
class EventBus:
    def subscribe(self, callback):
        self.listeners.append(callback)
'''
        patterns = parser.extract_patterns(source, "bus.py")
        registers = [p for p in patterns if p["pattern_type"] == "observer_register"]
        assert len(registers) == 1
        assert registers[0]["signal_name"] == "listeners"

    def test_emit_detected(self, parser):
        source = '''
class EventBus:
    def dispatch(self):
        for cb in self.handlers:
            cb()
'''
        patterns = parser.extract_patterns(source, "bus.py")
        emits = [p for p in patterns if p["pattern_type"] == "observer_emit"]
        assert len(emits) == 1
        assert emits[0]["signal_name"] == "handlers"

    def test_non_invoke_loop_ignored(self, parser):
        source = '''
class Printer:
    def show(self):
        for item in self.items:
            print(item)
'''
        patterns = parser.extract_patterns(source, "printer.py")
        emits = [p for p in patterns if p["pattern_type"] == "observer_emit"]
        assert len(emits) == 0

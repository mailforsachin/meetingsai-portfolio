def test_imports():
    from meetingsai_recorder import config, device, capture, logging_setup  # noqa: F401
    assert callable(logging_setup.setup_logging)
    assert callable(capture.ChunkCapturer)


def test_auto_gain_no_change_on_silence():
    import numpy as np
    from meetingsai_recorder.capture import auto_gain
    a = np.zeros(1600, dtype=np.int16)
    out = auto_gain(a)
    assert out.shape == a.shape
    assert out.dtype == a.dtype
    assert (out == 0).all()


def test_auto_gain_amplifies_quiet_signal():
    import numpy as np
    from meetingsai_recorder.capture import auto_gain
    a = np.full(1600, 100, dtype=np.int16)
    out = auto_gain(a)
    assert out.dtype == a.dtype
    assert int(np.max(np.abs(out))) > int(np.max(np.abs(a)))

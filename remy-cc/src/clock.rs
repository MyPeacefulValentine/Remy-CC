//! Injectable clock abstraction (design guideline §5.6: no time behavior may
//! depend on the real clock in tests).

use std::time::{Duration, SystemTime};

pub trait Clock: Send + Sync {
    fn now(&self) -> SystemTime;
    fn sleep(&self, duration: Duration);
}

pub struct SystemClock;

impl Clock for SystemClock {
    fn now(&self) -> SystemTime {
        SystemTime::now()
    }

    fn sleep(&self, duration: Duration) {
        std::thread::sleep(duration);
    }
}

#[cfg(test)]
pub mod fake {
    use std::sync::Mutex;
    use std::time::{Duration, SystemTime};

    use super::Clock;

    /// Deterministic clock: `sleep` advances `now` without real waiting.
    pub struct FakeClock {
        now: Mutex<SystemTime>,
        slept: Mutex<Vec<Duration>>,
    }

    impl FakeClock {
        pub fn new(start: SystemTime) -> Self {
            Self {
                now: Mutex::new(start),
                slept: Mutex::new(Vec::new()),
            }
        }

        pub fn slept_total(&self) -> Duration {
            self.slept.lock().unwrap().iter().sum()
        }
    }

    impl Clock for FakeClock {
        fn now(&self) -> SystemTime {
            *self.now.lock().unwrap()
        }

        fn sleep(&self, duration: Duration) {
            *self.now.lock().unwrap() += duration;
            self.slept.lock().unwrap().push(duration);
        }
    }
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    use super::fake::FakeClock;
    use super::Clock;

    #[test]
    fn fake_clock_sleep_advances_now() {
        let start = UNIX_EPOCH + Duration::from_secs(1_000_000);
        let clock = FakeClock::new(start);
        assert_eq!(clock.now(), start);

        clock.sleep(Duration::from_millis(1500));
        assert_eq!(clock.now(), start + Duration::from_millis(1500));

        clock.sleep(Duration::from_millis(500));
        assert_eq!(clock.now(), start + Duration::from_secs(2));
        assert_eq!(clock.slept_total(), Duration::from_secs(2));
    }

    #[test]
    fn system_clock_now_is_after_epoch() {
        let clock = super::SystemClock;
        assert!(clock.now() > SystemTime::UNIX_EPOCH);
    }
}

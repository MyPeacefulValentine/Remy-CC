//! Persistent job scheduler and the sole runtime owner of `StateStore`.

use std::io;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, Sender, SyncSender};
use std::sync::Arc;
use std::thread;

use crate::clock::Clock;
use crate::state::{
    CancelResult, Job, JobPriority, JobStatus, ListJobs, ProgressUpdate, PromoteResult, StateError,
    StateStore, SubmitJob, SubmitResult, PROVIDER_RUST,
};
use crate::worker::{self, WorkerEvent, WorkerOutcome};

pub type Reply<T> = SyncSender<Result<T, StateError>>;

pub enum Command {
    Submit(SubmitJob, Reply<crate::state::SubmitResult>),
    SubmitFullScan(i64, Reply<crate::state::SubmitResult>),
    Promote(i64, JobPriority, Reply<PromoteResult>),
    Get(i64, Reply<Job>),
    Cancel(i64, Reply<CancelResult>),
    List(ListJobs, Reply<Vec<Job>>),
    Status(Reply<(Vec<Job>, Vec<Job>)>),
    Worker(WorkerEvent),
    Shutdown,
}

#[derive(Clone)]
pub struct SchedulerHandle {
    sender: Sender<Command>,
}

impl SchedulerHandle {
    pub fn submit(&self, request: SubmitJob) -> Result<crate::state::SubmitResult, StateError> {
        self.request(|reply| Command::Submit(request, reply))
    }

    pub fn submit_full_scan(&self, project_id: i64) -> Result<SubmitResult, StateError> {
        self.request(|reply| Command::SubmitFullScan(project_id, reply))
    }

    pub fn promote(&self, job_id: i64, priority: JobPriority) -> Result<PromoteResult, StateError> {
        self.request(|reply| Command::Promote(job_id, priority, reply))
    }

    pub fn get(&self, job_id: i64) -> Result<Job, StateError> {
        self.request(|reply| Command::Get(job_id, reply))
    }

    pub fn cancel(&self, job_id: i64) -> Result<CancelResult, StateError> {
        self.request(|reply| Command::Cancel(job_id, reply))
    }

    pub fn list(&self, filter: ListJobs) -> Result<Vec<Job>, StateError> {
        self.request(|reply| Command::List(filter, reply))
    }

    pub fn status(&self) -> Result<(Vec<Job>, Vec<Job>), StateError> {
        self.request(Command::Status)
    }

    pub fn shutdown(&self) {
        let _ = self.sender.send(Command::Shutdown);
    }

    fn request<T>(&self, build: impl FnOnce(Reply<T>) -> Command) -> Result<T, StateError> {
        let (sender, receiver) = mpsc::sync_channel(1);
        self.sender
            .send(build(sender))
            .map_err(|_| StateError::Corrupt("scheduler channel is closed".to_string()))?;
        receiver
            .recv()
            .map_err(|_| StateError::Corrupt("scheduler reply channel is closed".to_string()))?
    }
}

pub fn start(
    store: StateStore,
    clock: Arc<dyn Clock>,
) -> io::Result<(SchedulerHandle, thread::JoinHandle<()>)> {
    let (sender, receiver) = mpsc::channel();
    let handle = SchedulerHandle {
        sender: sender.clone(),
    };
    let thread = thread::Builder::new()
        .name("remy-scheduler".to_string())
        .spawn(move || run(store, receiver, sender, clock))?;
    Ok((handle, thread))
}

struct ActiveJob {
    job_id: i64,
    cancel: Arc<AtomicBool>,
}

fn run(
    mut store: StateStore,
    receiver: Receiver<Command>,
    sender: Sender<Command>,
    _clock: Arc<dyn Clock>,
) {
    let mut active_job = None;
    if dispatch(&mut store, &sender, &mut active_job).is_err() {
        return;
    }
    while let Ok(command) = receiver.recv() {
        let result = match command {
            Command::Submit(request, reply) => {
                send_reply(reply, store.submit(request));
                Ok(())
            }
            Command::SubmitFullScan(project_id, reply) => {
                send_reply(reply, store.submit_full_scan(project_id));
                Ok(())
            }
            Command::Promote(job_id, priority, reply) => {
                send_reply(reply, store.promote(job_id, priority));
                Ok(())
            }
            Command::Get(job_id, reply) => {
                send_reply(reply, store.get(job_id));
                Ok(())
            }
            Command::Cancel(job_id, reply) => {
                let result = store.cancel(job_id);
                if let (Ok(outcome), Some(active)) = (&result, active_job.as_ref()) {
                    if outcome.changed
                        && outcome.job.status == JobStatus::CancelRequested
                        && active.job_id == job_id
                    {
                        active.cancel.store(true, Ordering::Relaxed);
                    }
                }
                send_reply(reply, result);
                Ok(())
            }
            Command::List(filter, reply) => {
                send_reply(reply, store.list(filter));
                Ok(())
            }
            Command::Status(reply) => {
                let active = store.list(ListJobs {
                    status: Some(JobStatus::Running),
                    limit: 200,
                    ..ListJobs::default()
                });
                let cancelling = store.list(ListJobs {
                    status: Some(JobStatus::CancelRequested),
                    limit: 200,
                    ..ListJobs::default()
                });
                let response = active.and_then(|mut jobs| {
                    jobs.extend(cancelling?);
                    Ok((jobs, store.recent_failed(10)?))
                });
                send_reply(reply, response);
                Ok(())
            }
            Command::Worker(event) => handle_worker(&mut store, event, &mut active_job),
            Command::Shutdown => break,
        };
        if result.is_err() || dispatch(&mut store, &sender, &mut active_job).is_err() {
            break;
        }
    }
}

fn send_reply<T>(reply: Reply<T>, result: Result<T, StateError>) {
    let _ = reply.send(result);
}

fn dispatch(
    store: &mut StateStore,
    sender: &Sender<Command>,
    active_job: &mut Option<ActiveJob>,
) -> Result<(), StateError> {
    if active_job.is_some() {
        return Ok(());
    }
    let Some(job) = store.claim_next_pending(PROVIDER_RUST)? else {
        return Ok(());
    };
    let (event_sender, event_receiver) = mpsc::channel();
    let command_sender = sender.clone();
    thread::spawn(move || {
        while let Ok(event) = event_receiver.recv() {
            if command_sender.send(Command::Worker(event)).is_err() {
                break;
            }
        }
    });
    match worker::spawn(job.clone(), event_sender) {
        Ok(cancel) => {
            *active_job = Some(ActiveJob {
                job_id: job.id,
                cancel,
            });
        }
        Err(error) => {
            *active_job = Some(ActiveJob {
                job_id: job.id,
                cancel: Arc::new(AtomicBool::new(false)),
            });
            sender
                .send(Command::Worker(WorkerEvent::Complete {
                    job_id: job.id,
                    outcome: WorkerOutcome::Failed(serde_json::json!({
                        "schema_version": 1,
                        "kind": "spawn_failed",
                        "message": error.to_string(),
                        "exit_code": null,
                        "stage": "runtime_validation",
                        "stderr": "",
                        "stderr_truncated": false,
                        "details": [],
                    })),
                }))
                .map_err(|_| StateError::Corrupt("scheduler channel is closed".to_string()))?;
        }
    }
    Ok(())
}

/// Terminal-state guard: a late worker event for a job
/// that already reached a terminal state is dropped instead of overwriting
/// it or killing the scheduler loop.
fn handle_worker(
    store: &mut StateStore,
    event: WorkerEvent,
    active_job: &mut Option<ActiveJob>,
) -> Result<(), StateError> {
    match event {
        WorkerEvent::Progress {
            job_id,
            current,
            message,
        } => {
            match store.update_progress(
                job_id,
                ProgressUpdate {
                    current,
                    total: 3,
                    message,
                },
            ) {
                Ok(_) => {}
                Err(StateError::InvalidTransition { .. }) | Err(StateError::NotFound(_)) => {}
                Err(error) => return Err(error),
            }
        }
        WorkerEvent::Complete { job_id, outcome } => {
            let current = store.get(job_id)?;
            match current.status {
                JobStatus::CancelRequested => {
                    store.confirm_cancelled(job_id)?;
                }
                JobStatus::Running => match outcome {
                    WorkerOutcome::Succeeded(result) => {
                        store.complete_success(job_id, result)?;
                    }
                    WorkerOutcome::Failed(error) => {
                        store.complete_failure(job_id, error)?;
                    }
                },
                _ => {}
            }
            if active_job
                .as_ref()
                .is_some_and(|active| active.job_id == job_id)
            {
                *active_job = None;
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::path::Path;
    use std::time::{Duration, SystemTime};

    use super::*;
    use crate::clock::fake::FakeClock;
    use crate::state::{JobPriority, SubmitJob};

    fn store_in(home: &Path) -> StateStore {
        let clock = Arc::new(FakeClock::new(
            SystemTime::UNIX_EPOCH + Duration::from_secs(1_750_000_000),
        ));
        StateStore::open(home, clock as Arc<dyn Clock>).unwrap().0
    }

    fn claimed_job(store: &mut StateStore, project: &Path, file: &str) -> Job {
        store
            .submit(SubmitJob {
                project_path: project.to_string_lossy().into_owned(),
                db_path: project
                    .join("logic_index.db")
                    .to_string_lossy()
                    .into_owned(),
                file_path: file.to_string(),
                priority: JobPriority::Interactive,
            })
            .unwrap();
        store.claim_next_pending(PROVIDER_RUST).unwrap().unwrap()
    }

    #[test]
    fn late_complete_after_terminal_state_is_dropped() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let mut store = store_in(home.path());
        let job = claimed_job(&mut store, project.path(), "a.py");
        let payload = serde_json::json!({"schema_version": 1, "outcome": "success"});
        store.complete_success(job.id, payload.clone()).unwrap();

        let mut active = Some(ActiveJob {
            job_id: job.id,
            cancel: Arc::new(AtomicBool::new(false)),
        });
        handle_worker(
            &mut store,
            WorkerEvent::Complete {
                job_id: job.id,
                outcome: WorkerOutcome::Failed(serde_json::json!({"late": true})),
            },
            &mut active,
        )
        .unwrap();
        let current = store.get(job.id).unwrap();
        assert_eq!(current.status, JobStatus::Succeeded);
        assert_eq!(current.result, Some(payload));
        assert!(active.is_none());
    }

    #[test]
    fn cancel_requested_complete_confirms_cancellation_over_worker_outcome() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let mut store = store_in(home.path());
        let job = claimed_job(&mut store, project.path(), "a.py");
        assert!(store.cancel(job.id).unwrap().changed);

        let mut active = Some(ActiveJob {
            job_id: job.id,
            cancel: Arc::new(AtomicBool::new(false)),
        });
        handle_worker(
            &mut store,
            WorkerEvent::Complete {
                job_id: job.id,
                outcome: WorkerOutcome::Succeeded(
                    serde_json::json!({"schema_version": 1, "outcome": "success"}),
                ),
            },
            &mut active,
        )
        .unwrap();
        assert_eq!(store.get(job.id).unwrap().status, JobStatus::Cancelled);
    }

    #[test]
    fn progress_after_terminal_state_is_ignored() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let mut store = store_in(home.path());
        let job = claimed_job(&mut store, project.path(), "a.py");
        store
            .complete_success(job.id, serde_json::json!({"schema_version": 1}))
            .unwrap();

        let mut active = None;
        handle_worker(
            &mut store,
            WorkerEvent::Progress {
                job_id: job.id,
                current: 2,
                message: "scanning".to_string(),
            },
            &mut active,
        )
        .unwrap();
        assert_eq!(store.get(job.id).unwrap().status, JobStatus::Succeeded);
    }

    #[test]
    fn single_slot_never_claims_a_successor_of_the_running_job() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let mut store = store_in(home.path());
        let job = claimed_job(&mut store, project.path(), "a.py");
        store
            .submit(SubmitJob {
                project_path: project.path().to_string_lossy().into_owned(),
                db_path: project
                    .path()
                    .join("logic_index.db")
                    .to_string_lossy()
                    .into_owned(),
                file_path: "a.py".to_string(),
                priority: JobPriority::Interactive,
            })
            .unwrap();
        assert!(store.claim_next_pending(PROVIDER_RUST).unwrap().is_none());
        assert_eq!(store.get(job.id).unwrap().status, JobStatus::Running);
    }
}

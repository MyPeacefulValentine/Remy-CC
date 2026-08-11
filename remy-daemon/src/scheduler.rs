//! Persistent job scheduler and the sole runtime owner of `StateStore`.

use std::io;
use std::sync::mpsc::{self, Receiver, Sender, SyncSender};
use std::sync::Arc;
use std::thread;

use crate::clock::Clock;
use crate::state::{
    CancelResult, Job, JobStatus, ListJobs, ProgressUpdate, StateError, StateStore, SubmitJob,
};
use crate::worker::{self, WorkerEvent, WorkerOutcome};

pub type Reply<T> = SyncSender<Result<T, StateError>>;

pub enum Command {
    Submit(SubmitJob, Reply<crate::state::SubmitResult>),
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
            Command::Get(job_id, reply) => {
                send_reply(reply, store.get(job_id));
                Ok(())
            }
            Command::Cancel(job_id, reply) => {
                send_reply(reply, store.cancel(job_id));
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
    active_job: &mut Option<i64>,
) -> Result<(), StateError> {
    if active_job.is_some() {
        return Ok(());
    }
    let Some(job) = store.claim_next_pending()? else {
        return Ok(());
    };
    *active_job = Some(job.id);
    let (event_sender, event_receiver) = mpsc::channel();
    let command_sender = sender.clone();
    thread::spawn(move || {
        while let Ok(event) = event_receiver.recv() {
            if command_sender.send(Command::Worker(event)).is_err() {
                break;
            }
        }
    });
    if let Err(error) = worker::spawn(job.clone(), event_sender) {
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
    Ok(())
}

fn handle_worker(
    store: &mut StateStore,
    event: WorkerEvent,
    active_job: &mut Option<i64>,
) -> Result<(), StateError> {
    match event {
        WorkerEvent::Progress {
            job_id,
            current,
            message,
        } => {
            store.update_progress(
                job_id,
                ProgressUpdate {
                    current,
                    total: 3,
                    message,
                },
            )?;
        }
        WorkerEvent::Complete { job_id, outcome } => {
            let current = store.get(job_id)?;
            if current.status == JobStatus::CancelRequested {
                store.confirm_cancelled(job_id)?;
            } else {
                match outcome {
                    WorkerOutcome::Succeeded(result) => {
                        store.complete_success(job_id, result)?;
                    }
                    WorkerOutcome::Failed(error) => {
                        store.complete_failure(job_id, error)?;
                    }
                }
            }
            if *active_job == Some(job_id) {
                *active_job = None;
            }
        }
    }
    Ok(())
}

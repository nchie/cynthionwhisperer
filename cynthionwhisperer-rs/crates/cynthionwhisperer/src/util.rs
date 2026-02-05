//! Utility code that doesn't belong anywhere specific.

use anyhow::{Error, bail};

pub fn handle_thread_panic<T>(result: std::thread::Result<T>)
    -> Result<T, Error>
{
    match result {
        Ok(x) => Ok(x),
        Err(panic) => {
            let msg = match (
                panic.downcast_ref::<&str>(),
                panic.downcast_ref::<String>())
            {
                (Some(&s), _) => s,
                (_,  Some(s)) => s,
                (None,  None) => "<No panic message>"
            };
            bail!("Worker thread panic: {msg}");
        }
    }
}

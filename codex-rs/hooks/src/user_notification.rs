use std::path::Path;
use std::process::Stdio;
use std::sync::Arc;

use serde::Deserialize;
use serde::Serialize;

use crate::Hook;
use crate::HookDirective;
use crate::HookEvent;
use crate::HookPayload;
use crate::HookResult;
use crate::command_from_argv;

/// Legacy notify payload appended as the final argv argument for backward compatibility.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "type", rename_all = "kebab-case")]
enum UserNotification {
    #[serde(rename_all = "kebab-case")]
    AgentTurnComplete {
        thread_id: String,
        turn_id: String,
        cwd: String,

        /// Messages that the user sent to the agent to initiate the turn.
        input_messages: Vec<String>,

        /// The last message sent by the assistant in the turn.
        last_assistant_message: Option<String>,
    },
}

pub fn legacy_notify_json(hook_event: &HookEvent, cwd: &Path) -> Result<String, serde_json::Error> {
    match hook_event {
        HookEvent::AfterAgent { event } => {
            serde_json::to_string(&UserNotification::AgentTurnComplete {
                thread_id: event.thread_id.to_string(),
                turn_id: event.turn_id.clone(),
                cwd: cwd.display().to_string(),
                input_messages: event.input_messages.clone(),
                last_assistant_message: event.last_assistant_message.clone(),
            })
        }
        _ => Err(serde_json::Error::io(std::io::Error::other(
            "legacy notify payload is only supported for after_agent",
        ))),
    }
}

pub fn notify_hook(argv: Vec<String>) -> Hook {
    let argv = Arc::new(argv);
    Hook {
        name: "legacy_notify".to_string(),
        func: Arc::new(move |payload: &HookPayload| {
            let argv = Arc::clone(&argv);
            Box::pin(async move {
                let mut command = match command_from_argv(&argv) {
                    Some(command) => command,
                    None => return HookResult::Success,
                };
                if let Ok(notify_payload) = legacy_notify_json(&payload.hook_event, &payload.cwd) {
                    command.arg(notify_payload);
                }

                // Backwards-compat: match legacy notify behavior (argv + JSON arg, fire-and-forget).
                command
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null());

                match command.spawn() {
                    Ok(_) => HookResult::Success,
                    Err(err) => HookResult::FailedContinue(err.into()),
                }
            })
        }),
    }
}

#[derive(Debug, Deserialize)]
struct NextTurnDirectiveWire {
    need_next_turn: Option<bool>,
    next_turn_input: Option<String>,
    reason: Option<String>,
}

fn parse_next_turn_directive(stdout: &[u8]) -> Option<HookDirective> {
    let text = String::from_utf8_lossy(stdout).trim().to_string();
    if text.is_empty() {
        return None;
    }

    if let Ok(parsed) = serde_json::from_str::<NextTurnDirectiveWire>(&text) {
        let need = parsed.need_next_turn.unwrap_or(false);
        let input = parsed.next_turn_input.unwrap_or_default();
        let trimmed = input.trim().to_string();
        let reason = parsed.reason.and_then(|v| {
            let t = v.trim().to_string();
            if t.is_empty() {
                None
            } else {
                Some(t)
            }
        });
        if need && !trimmed.is_empty() {
            return Some(HookDirective::QueueNextTurn {
                input: trimmed,
                reason,
            });
        }
        if let Some(reason) = reason {
            return Some(HookDirective::Notify {
                message: format!("Auto next-turn decision: {reason}"),
            });
        }
        return None;
    }

    // Accept plain-text stdout as the next-turn prompt.
    Some(HookDirective::QueueNextTurn {
        input: text,
        reason: None,
    })
}

pub fn notify_next_turn_hook(argv: Vec<String>, service_url: Option<String>) -> Hook {
    let argv = Arc::new(argv);
    let service_url = Arc::new(service_url);
    Hook {
        name: "legacy_notify_next_turn".to_string(),
        func: Arc::new(move |payload: &HookPayload| {
            let argv = Arc::clone(&argv);
            let service_url = Arc::clone(&service_url);
            Box::pin(async move {
                let mut command = match command_from_argv(&argv) {
                    Some(command) => command,
                    None => return HookResult::Success,
                };
                if let Some(url) = service_url.as_deref() {
                    command.env("AUTO_NEXT_SERVICE_URL", url);
                }
                if let Ok(notify_payload) = legacy_notify_json(&payload.hook_event, &payload.cwd) {
                    command.arg(notify_payload);
                }

                command.stdin(Stdio::null()).stderr(Stdio::null());

                match command.output().await {
                    Ok(output) => {
                        if !output.status.success() {
                            return HookResult::FailedContinue(
                                std::io::Error::other(format!(
                                    "next-turn hook exited with status {}",
                                    output.status
                                ))
                                .into(),
                            );
                        }
                        if let Some(directive) = parse_next_turn_directive(&output.stdout) {
                            HookResult::SuccessWithDirective(directive)
                        } else {
                            HookResult::Success
                        }
                    }
                    Err(err) => HookResult::FailedContinue(err.into()),
                }
            })
        }),
    }
}

#[cfg(test)]
mod tests {
    use anyhow::Result;
    use codex_protocol::ThreadId;
    use pretty_assertions::assert_eq;
    use serde_json::Value;
    use serde_json::json;

    use super::*;

    fn expected_notification_json() -> Value {
        json!({
            "type": "agent-turn-complete",
            "thread-id": "b5f6c1c2-1111-2222-3333-444455556666",
            "turn-id": "12345",
            "cwd": "/Users/example/project",
            "input-messages": ["Rename `foo` to `bar` and update the callsites."],
            "last-assistant-message": "Rename complete and verified `cargo build` succeeds.",
        })
    }

    #[test]
    fn test_user_notification() -> Result<()> {
        let notification = UserNotification::AgentTurnComplete {
            thread_id: "b5f6c1c2-1111-2222-3333-444455556666".to_string(),
            turn_id: "12345".to_string(),
            cwd: "/Users/example/project".to_string(),
            input_messages: vec!["Rename `foo` to `bar` and update the callsites.".to_string()],
            last_assistant_message: Some(
                "Rename complete and verified `cargo build` succeeds.".to_string(),
            ),
        };
        let serialized = serde_json::to_string(&notification)?;
        let actual: Value = serde_json::from_str(&serialized)?;
        assert_eq!(actual, expected_notification_json());
        Ok(())
    }

    #[test]
    fn legacy_notify_json_matches_historical_wire_shape() -> Result<()> {
        let hook_event = HookEvent::AfterAgent {
            event: crate::HookEventAfterAgent {
                thread_id: ThreadId::from_string("b5f6c1c2-1111-2222-3333-444455556666")
                    .expect("valid thread id"),
                turn_id: "12345".to_string(),
                input_messages: vec!["Rename `foo` to `bar` and update the callsites.".to_string()],
                last_assistant_message: Some(
                    "Rename complete and verified `cargo build` succeeds.".to_string(),
                ),
            },
        };

        let serialized = legacy_notify_json(&hook_event, Path::new("/Users/example/project"))?;
        let actual: Value = serde_json::from_str(&serialized)?;
        assert_eq!(actual, expected_notification_json());

        Ok(())
    }

    #[test]
    fn parse_next_turn_directive_accepts_json_shape() {
        let stdout = br#"{"need_next_turn":true,"next_turn_input":"run tests"}"#;
        let parsed = parse_next_turn_directive(stdout);
        assert_eq!(
            parsed,
            Some(HookDirective::QueueNextTurn {
                input: "run tests".to_string(),
                reason: None,
            })
        );
    }

    #[test]
    fn parse_next_turn_directive_parses_reason() {
        let stdout = br#"{"need_next_turn":true,"next_turn_input":"run tests","reason":"missing verification"}"#;
        let parsed = parse_next_turn_directive(stdout);
        assert_eq!(
            parsed,
            Some(HookDirective::QueueNextTurn {
                input: "run tests".to_string(),
                reason: Some("missing verification".to_string()),
            })
        );
    }

    #[test]
    fn parse_next_turn_directive_notifies_reason_when_no_continuation() {
        let stdout = br#"{"need_next_turn":false,"next_turn_input":"","reason":"already complete"}"#;
        let parsed = parse_next_turn_directive(stdout);
        assert_eq!(
            parsed,
            Some(HookDirective::Notify {
                message: "Auto next-turn decision: already complete".to_string(),
            })
        );
    }

    #[test]
    fn parse_next_turn_directive_accepts_plain_text() {
        let stdout = b"run tests and summarize\n";
        let parsed = parse_next_turn_directive(stdout);
        assert_eq!(
            parsed,
            Some(HookDirective::QueueNextTurn {
                input: "run tests and summarize".to_string(),
                reason: None,
            })
        );
    }
}

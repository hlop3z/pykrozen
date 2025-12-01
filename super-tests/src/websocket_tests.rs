//! WebSocket endpoint testing module.

use std::time::{Duration, Instant};

use colored::Colorize;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::time::timeout;
use tokio_tungstenite::{connect_async, tungstenite::Message};

use crate::config::Config;

/// WebSocket test result.
#[derive(Debug)]
pub struct WebSocketTestResult {
    pub test_name: String,
    pub messages_sent: usize,
    pub messages_received: usize,
    pub latency_ms: f64,
    pub success: bool,
    pub error: Option<String>,
}

impl WebSocketTestResult {
    fn print(&self) {
        let result_icon = if self.success {
            "✓".green()
        } else {
            "✗".red()
        };

        println!(
            "  {} {} - sent: {}, received: {}, latency: {}ms",
            result_icon,
            self.test_name.white(),
            self.messages_sent.to_string().cyan(),
            self.messages_received.to_string().cyan(),
            format!("{:.2}", self.latency_ms).yellow()
        );

        if let Some(ref err) = self.error {
            println!("      {} {}", "Error:".red(), err);
        }
    }
}

/// Run comprehensive WebSocket tests.
pub async fn run_websocket_tests(config: &Config, num_connections: usize, messages_per_conn: usize) {
    let ws_url = config.ws_url();

    println!(
        "  Testing WebSocket at {}...\n",
        ws_url.cyan()
    );

    let mut results = Vec::new();

    // Test 1: Basic connection
    let basic_result = test_basic_connection(&ws_url).await;
    basic_result.print();
    results.push(basic_result);

    // Test 2: Echo functionality
    let echo_result = test_echo(&ws_url).await;
    echo_result.print();
    results.push(echo_result);

    // Test 3: JSON message handling
    let json_result = test_json_messages(&ws_url).await;
    json_result.print();
    results.push(json_result);

    // Test 4: Multiple messages
    let multi_result = test_multiple_messages(&ws_url, 10).await;
    multi_result.print();
    results.push(multi_result);

    // Test 5: Concurrent connections
    let concurrent_result = test_concurrent_connections(&ws_url, num_connections, messages_per_conn).await;
    concurrent_result.print();
    results.push(concurrent_result);

    // Test 6: Large payload
    let large_result = test_large_payload(&ws_url).await;
    large_result.print();
    results.push(large_result);

    // Test 7: Rapid fire messages
    let rapid_result = test_rapid_fire(&ws_url, 100).await;
    rapid_result.print();
    results.push(rapid_result);

    // Test 8: Connection stability (connect/disconnect cycles)
    let stability_result = test_connection_stability(&ws_url, 5).await;
    stability_result.print();
    results.push(stability_result);

    print_summary(&results);
}

/// Test basic WebSocket connection.
async fn test_basic_connection(ws_url: &str) -> WebSocketTestResult {
    let start = Instant::now();

    match timeout(Duration::from_secs(5), connect_async(ws_url)).await {
        Ok(Ok((mut ws, _))) => {
            let _ = ws.close(None).await;
            WebSocketTestResult {
                test_name: "Basic Connection".to_string(),
                messages_sent: 0,
                messages_received: 0,
                latency_ms: start.elapsed().as_secs_f64() * 1000.0,
                success: true,
                error: None,
            }
        }
        Ok(Err(e)) => WebSocketTestResult {
            test_name: "Basic Connection".to_string(),
            messages_sent: 0,
            messages_received: 0,
            latency_ms: start.elapsed().as_secs_f64() * 1000.0,
            success: false,
            error: Some(e.to_string()),
        },
        Err(_) => WebSocketTestResult {
            test_name: "Basic Connection".to_string(),
            messages_sent: 0,
            messages_received: 0,
            latency_ms: start.elapsed().as_secs_f64() * 1000.0,
            success: false,
            error: Some("Connection timeout".to_string()),
        },
    }
}

/// Test WebSocket echo functionality.
async fn test_echo(ws_url: &str) -> WebSocketTestResult {
    let start = Instant::now();

    match timeout(Duration::from_secs(10), async {
        let (mut ws, _) = connect_async(ws_url).await?;

        let test_message = json!({"test": "echo", "value": 42});
        ws.send(Message::Text(test_message.to_string())).await?;

        if let Some(Ok(msg)) = ws.next().await {
            if let Message::Text(text) = msg {
                let response: Value = serde_json::from_str(&text)?;
                // pykrozen echoes back as {"echo": <original_message>}
                if response.get("echo").is_some() {
                    ws.close(None).await?;
                    return Ok::<_, Box<dyn std::error::Error + Send + Sync>>((1, 1, true));
                }
            }
        }

        ws.close(None).await?;
        Ok((1, 0, false))
    })
    .await
    {
        Ok(Ok((sent, received, success))) => WebSocketTestResult {
            test_name: "Echo Test".to_string(),
            messages_sent: sent,
            messages_received: received,
            latency_ms: start.elapsed().as_secs_f64() * 1000.0,
            success,
            error: if success {
                None
            } else {
                Some("Echo response not received".to_string())
            },
        },
        Ok(Err(e)) => WebSocketTestResult {
            test_name: "Echo Test".to_string(),
            messages_sent: 0,
            messages_received: 0,
            latency_ms: start.elapsed().as_secs_f64() * 1000.0,
            success: false,
            error: Some(e.to_string()),
        },
        Err(_) => WebSocketTestResult {
            test_name: "Echo Test".to_string(),
            messages_sent: 0,
            messages_received: 0,
            latency_ms: start.elapsed().as_secs_f64() * 1000.0,
            success: false,
            error: Some("Test timeout".to_string()),
        },
    }
}

/// Test JSON message handling.
async fn test_json_messages(ws_url: &str) -> WebSocketTestResult {
    let start = Instant::now();

    match timeout(Duration::from_secs(10), async {
        let (mut ws, _) = connect_async(ws_url).await?;

        let messages = vec![
            json!({"type": "simple", "data": "hello"}),
            json!({"type": "nested", "data": {"inner": {"deep": true}}}),
            json!({"type": "array", "items": [1, 2, 3, 4, 5]}),
            json!({"type": "mixed", "string": "test", "number": 123, "bool": false, "null": null}),
        ];

        let mut sent = 0;
        let mut received = 0;

        for msg in &messages {
            ws.send(Message::Text(msg.to_string())).await?;
            sent += 1;

            if let Some(Ok(Message::Text(_))) = ws.next().await {
                received += 1;
            }
        }

        ws.close(None).await?;
        Ok::<_, Box<dyn std::error::Error + Send + Sync>>((sent, received))
    })
    .await
    {
        Ok(Ok((sent, received))) => WebSocketTestResult {
            test_name: "JSON Messages".to_string(),
            messages_sent: sent,
            messages_received: received,
            latency_ms: start.elapsed().as_secs_f64() * 1000.0,
            success: sent == received,
            error: if sent == received {
                None
            } else {
                Some(format!("Message mismatch: sent {} but received {}", sent, received))
            },
        },
        Ok(Err(e)) => WebSocketTestResult {
            test_name: "JSON Messages".to_string(),
            messages_sent: 0,
            messages_received: 0,
            latency_ms: start.elapsed().as_secs_f64() * 1000.0,
            success: false,
            error: Some(e.to_string()),
        },
        Err(_) => WebSocketTestResult {
            test_name: "JSON Messages".to_string(),
            messages_sent: 0,
            messages_received: 0,
            latency_ms: start.elapsed().as_secs_f64() * 1000.0,
            success: false,
            error: Some("Test timeout".to_string()),
        },
    }
}

/// Test sending multiple messages.
async fn test_multiple_messages(ws_url: &str, count: usize) -> WebSocketTestResult {
    let start = Instant::now();

    match timeout(Duration::from_secs(30), async {
        let (mut ws, _) = connect_async(ws_url).await?;

        let mut sent = 0;
        let mut received = 0;

        for i in 0..count {
            let msg = json!({"seq": i, "data": format!("message_{}", i)});
            ws.send(Message::Text(msg.to_string())).await?;
            sent += 1;

            if let Some(Ok(Message::Text(_))) = ws.next().await {
                received += 1;
            }
        }

        ws.close(None).await?;
        Ok::<_, Box<dyn std::error::Error + Send + Sync>>((sent, received))
    })
    .await
    {
        Ok(Ok((sent, received))) => WebSocketTestResult {
            test_name: format!("Multiple Messages ({})", count),
            messages_sent: sent,
            messages_received: received,
            latency_ms: start.elapsed().as_secs_f64() * 1000.0,
            success: sent == received,
            error: None,
        },
        Ok(Err(e)) => WebSocketTestResult {
            test_name: format!("Multiple Messages ({})", count),
            messages_sent: 0,
            messages_received: 0,
            latency_ms: start.elapsed().as_secs_f64() * 1000.0,
            success: false,
            error: Some(e.to_string()),
        },
        Err(_) => WebSocketTestResult {
            test_name: format!("Multiple Messages ({})", count),
            messages_sent: 0,
            messages_received: 0,
            latency_ms: start.elapsed().as_secs_f64() * 1000.0,
            success: false,
            error: Some("Test timeout".to_string()),
        },
    }
}

/// Test concurrent WebSocket connections.
async fn test_concurrent_connections(
    ws_url: &str,
    num_connections: usize,
    messages_per_conn: usize,
) -> WebSocketTestResult {
    let start = Instant::now();

    let ws_url = ws_url.to_string();
    let handles: Vec<_> = (0..num_connections)
        .map(|_| {
            let url = ws_url.clone();
            tokio::spawn(async move {
                match timeout(Duration::from_secs(30), async {
                    let (mut ws, _) = connect_async(&url).await?;

                    let mut sent = 0;
                    let mut received = 0;

                    for i in 0..messages_per_conn {
                        let msg = json!({"index": i});
                        ws.send(Message::Text(msg.to_string())).await?;
                        sent += 1;

                        if let Some(Ok(Message::Text(_))) = ws.next().await {
                            received += 1;
                        }
                    }

                    ws.close(None).await?;
                    Ok::<_, Box<dyn std::error::Error + Send + Sync>>((sent, received))
                })
                .await
                {
                    Ok(Ok(result)) => result,
                    _ => (0, 0),
                }
            })
        })
        .collect();

    let results: Vec<_> = futures_util::future::join_all(handles)
        .await
        .into_iter()
        .filter_map(|r| r.ok())
        .collect();

    let total_sent: usize = results.iter().map(|(s, _)| s).sum();
    let total_received: usize = results.iter().map(|(_, r)| r).sum();
    let successful_connections = results.len();

    WebSocketTestResult {
        test_name: format!("Concurrent ({} connections)", num_connections),
        messages_sent: total_sent,
        messages_received: total_received,
        latency_ms: start.elapsed().as_secs_f64() * 1000.0,
        success: successful_connections == num_connections && total_sent == total_received,
        error: if successful_connections < num_connections {
            Some(format!(
                "Only {} of {} connections succeeded",
                successful_connections, num_connections
            ))
        } else {
            None
        },
    }
}

/// Test with large payload.
async fn test_large_payload(ws_url: &str) -> WebSocketTestResult {
    let start = Instant::now();

    match timeout(Duration::from_secs(30), async {
        let (mut ws, _) = connect_async(ws_url).await?;

        // Send a large message (~100KB)
        let large_data = "x".repeat(100_000);
        let msg = json!({"type": "large", "data": large_data});
        ws.send(Message::Text(msg.to_string())).await?;

        let received = if let Some(Ok(Message::Text(_))) = ws.next().await {
            1
        } else {
            0
        };

        ws.close(None).await?;
        Ok::<_, Box<dyn std::error::Error + Send + Sync>>((1, received))
    })
    .await
    {
        Ok(Ok((sent, received))) => WebSocketTestResult {
            test_name: "Large Payload (~100KB)".to_string(),
            messages_sent: sent,
            messages_received: received,
            latency_ms: start.elapsed().as_secs_f64() * 1000.0,
            success: received == 1,
            error: None,
        },
        Ok(Err(e)) => WebSocketTestResult {
            test_name: "Large Payload (~100KB)".to_string(),
            messages_sent: 0,
            messages_received: 0,
            latency_ms: start.elapsed().as_secs_f64() * 1000.0,
            success: false,
            error: Some(e.to_string()),
        },
        Err(_) => WebSocketTestResult {
            test_name: "Large Payload (~100KB)".to_string(),
            messages_sent: 0,
            messages_received: 0,
            latency_ms: start.elapsed().as_secs_f64() * 1000.0,
            success: false,
            error: Some("Test timeout".to_string()),
        },
    }
}

/// Test rapid-fire message sending.
async fn test_rapid_fire(ws_url: &str, count: usize) -> WebSocketTestResult {
    let start = Instant::now();

    match timeout(Duration::from_secs(30), async {
        let (mut ws, _) = connect_async(ws_url).await?;

        // Send all messages rapidly
        for i in 0..count {
            let msg = json!({"rapid": i});
            ws.send(Message::Text(msg.to_string())).await?;
        }

        // Collect responses
        let mut received = 0;
        for _ in 0..count {
            match timeout(Duration::from_millis(500), ws.next()).await {
                Ok(Some(Ok(Message::Text(_)))) => received += 1,
                _ => break,
            }
        }

        ws.close(None).await?;
        Ok::<_, Box<dyn std::error::Error + Send + Sync>>((count, received))
    })
    .await
    {
        Ok(Ok((sent, received))) => WebSocketTestResult {
            test_name: format!("Rapid Fire ({} messages)", count),
            messages_sent: sent,
            messages_received: received,
            latency_ms: start.elapsed().as_secs_f64() * 1000.0,
            success: received >= sent / 2, // Allow some message loss in rapid fire
            error: if received < sent {
                Some(format!(
                    "Lost {} messages ({:.1}%)",
                    sent - received,
                    ((sent - received) as f64 / sent as f64) * 100.0
                ))
            } else {
                None
            },
        },
        Ok(Err(e)) => WebSocketTestResult {
            test_name: format!("Rapid Fire ({} messages)", count),
            messages_sent: 0,
            messages_received: 0,
            latency_ms: start.elapsed().as_secs_f64() * 1000.0,
            success: false,
            error: Some(e.to_string()),
        },
        Err(_) => WebSocketTestResult {
            test_name: format!("Rapid Fire ({} messages)", count),
            messages_sent: 0,
            messages_received: 0,
            latency_ms: start.elapsed().as_secs_f64() * 1000.0,
            success: false,
            error: Some("Test timeout".to_string()),
        },
    }
}

/// Test connection stability with connect/disconnect cycles.
async fn test_connection_stability(ws_url: &str, cycles: usize) -> WebSocketTestResult {
    let start = Instant::now();
    let mut successful_cycles = 0;

    for _ in 0..cycles {
        match timeout(Duration::from_secs(5), async {
            let (mut ws, _) = connect_async(ws_url).await?;

            // Send a message
            let msg = json!({"test": "stability"});
            ws.send(Message::Text(msg.to_string())).await?;

            // Wait for response
            if let Some(Ok(Message::Text(_))) = ws.next().await {
                ws.close(None).await?;
                return Ok::<_, Box<dyn std::error::Error + Send + Sync>>(true);
            }

            ws.close(None).await?;
            Ok(false)
        })
        .await
        {
            Ok(Ok(true)) => successful_cycles += 1,
            _ => {}
        }

        // Small delay between cycles
        tokio::time::sleep(Duration::from_millis(100)).await;
    }

    WebSocketTestResult {
        test_name: format!("Connection Stability ({} cycles)", cycles),
        messages_sent: successful_cycles,
        messages_received: successful_cycles,
        latency_ms: start.elapsed().as_secs_f64() * 1000.0,
        success: successful_cycles == cycles,
        error: if successful_cycles < cycles {
            Some(format!(
                "Only {} of {} cycles succeeded",
                successful_cycles, cycles
            ))
        } else {
            None
        },
    }
}

/// Print summary of WebSocket test results.
fn print_summary(results: &[WebSocketTestResult]) {
    let total = results.len();
    let success = results.iter().filter(|r| r.success).count();
    let failed = total - success;

    let total_latency: f64 = results.iter().map(|r| r.latency_ms).sum();
    let avg_latency = if total > 0 {
        total_latency / total as f64
    } else {
        0.0
    };

    let total_sent: usize = results.iter().map(|r| r.messages_sent).sum();
    let total_received: usize = results.iter().map(|r| r.messages_received).sum();

    println!();
    println!("  {}", "─── Summary ───".white().bold());
    println!(
        "    Tests: {} | {} {} | {} {}",
        total.to_string().white(),
        success.to_string().green(),
        "passed".green(),
        failed.to_string().red(),
        "failed".red()
    );
    println!(
        "    Messages: {} sent, {} received",
        total_sent.to_string().cyan(),
        total_received.to_string().cyan()
    );
    println!(
        "    Avg Latency: {}",
        format!("{:.2}ms", avg_latency).yellow()
    );
}

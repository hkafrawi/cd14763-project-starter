"""
Customer Support AI Agent — Starter Code
==========================================
Your task is to complete this file by implementing all sections marked
with # TODO comments.

Reference the step-by-step solution files and INSTRUCTIONS.md for guidance.
Do NOT copy the solution directly — work through each section yourself.

Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
# These imports are provided. Do not remove them.
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from dotenv import load_dotenv
import shutil

from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")

app = BedrockAgentCoreApp()  


# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


GATEWAY_URL = os.getenv("GATEWAY_URL")
KB_ID       = os.getenv("KB_ID")
REGION      = os.getenv("REGION")
MEMORY_ID   = os.getenv("MEMORY_ID")



model_id = "global.amazon.nova-2-lite-v1:0"
model = BedrockModel(model_id=model_id, temperature=0.7, max_tokens=1024)  
memory_client = MemoryClient(region_name=REGION)   

_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    strategies = mem_client.get_memory_strategies(memory_id)
    return {strategy["type"]: strategy["namespaces"][0] for strategy in strategies}

class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.namespaces = get_namespaces(self.memory_client, self.memory_id)


    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""

        # only runs for plain-text user messages (not tool results)
        if not self.actor_id:
            return

        messages = event.agent.messages
        if (
            not messages
            or messages[-1]["role"] != "user"
            or "toolResult" in messages[-1]["content"][0]
        ):
            return

        user_query = messages[-1]["content"][0]["text"]

        try:
            all_context = []
            for strategy_type, namespace in self.namespaces.items():
                resolved_namespace = namespace.format(actorId=self.actor_id)
                memories = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=resolved_namespace,
                    query=user_query,
                    top_k=5
                )
                for memory in memories:
                    if isinstance(memory, dict):
                        text = memory.get("content",{}).get("text","").strip()
                        if text:
                            all_context.append(f"[{strategy_type}] {text}")

            if all_context:
                context_block = "\n".join(all_context)
                og_text = messages[-1]["content"][0]["text"]
                messages[-1]["content"][0]["text"] = (
                    f"Customer Context:\n{context_block}\n\n{og_text}")
                logger.info(f"Retrieved {len(all_context)} memory items for actor {self.actor_id}")

        except Exception as e:
            logger.error(f"Error at context retrieval: {e}")


    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""

        if not self.actor_id or not self.session_id:
            return

        try:
            messages = event.agent.messages
            user_text = agent_text = None

            for message in reversed(messages):
                if message["role"] == "assistant" and not agent_text:
                    content = message["content"]
                    if isinstance(content, list):
                        agent_text = content[0].get("text","")
                    else:
                        agent_text = str(content)
                elif (message["role"] == "user"
                      and not user_text
                      and "toolResult" not in message["content"][0]):
                    user_text = message["content"][0]["text"]
                    break

            if user_text and agent_text:
                self.memory_client.create_event(
                    memory_id=self.memory_id,
                    actor_id=self.actor_id,
                    session_id=self.session_id,
                    messages=[
                        (user_text, "USER"),
                        (agent_text, "ASSISTANT")
                    ]
                )
                logger.info(f"Interaction Saved for {self.actor_id}")
        except Exception as e:
            logger.error(f"Error in saving interaction: {e}")


    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    if not KB_ID:
        logger.error("Knowledge base not configured")
        return

    response = _bedrock_runtime.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={"text":query}
    )
    results = response.get("retrievalResults",[])
    if not results:
        logger.info(f"No Information found for: {query}")
        return f"No information found for: {query}"
    chunks = [result["content"]["text"] for result in results]
    return "\n---\n".join(chunks)


@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    code = f"""
            import json

            earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
            tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}
            point_value = 0.01  # $ per point

            loyalty_points = {loyalty_points}
            tier = "{tier}"
            order_total = {order_total}
            product_category = "{product_category}"

            max_redeemable_value = order_total * 0.5
            max_points_by_cap = int(max_redeemable_value / point_value)
            points_redeemed = min(loyalty_points, max_points_by_cap)
            points_redeemed = (points_redeemed // 500) * 500
            points_discount = points_redeemed * point_value

            subtotal_after_points = order_total - points_discount

            tier_discount_pct = tier_rates.get(tier, 0.0)
            tier_discount = subtotal_after_points * tier_discount_pct

            final_total = subtotal_after_points - tier_discount
            total_savings = order_total - final_total

            earn_rate = earn_rates.get(product_category, 1)
            points_earned = int(final_total * earn_rate)
            remaining_points = loyalty_points - points_redeemed + points_earned

            result = {{
                "order_total": round(order_total, 2),
                "points_redeemed": points_redeemed,
                "points_discount": round(points_discount, 2),
                "tier": tier,
                "tier_discount_pct": tier_discount_pct,
                "tier_discount": round(tier_discount, 2),
                "final_total": round(final_total, 2),
                "total_savings": round(total_savings, 2),
                "points_earned": points_earned,
                "remaining_points": remaining_points,
            }}

            print(json.dumps(result))
            """

    try:
        with code_session(REGION) as code_client:
            response = code_client.invoke(
                "executeCode",{
                    "code": code,
                    "language": "python",
                    "clearContext": True
                }
            )
        for event in response["stream"]:
            return json.dumps(event["result"])

    except Exception as e:
       logging.error(f"Code Interperter unavailable with the following error: {e}")
       tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
       tier_discount_rate = tier_rates.get(tier, 0.0)
       tier_discount = order_total * tier_discount_rate
       final_total = order_total - tier_discount

       return json.dumps({
            "final_total": round(final_total, 2),
            "total_savings": round(tier_discount, 2),
            "tier": tier,
            "tier_discount_rate": tier_discount_rate
        })

SYSTEM_PROMPT = """
You are a customer support AI agent for an e-commerce platform. You help
customers with order tracking, returns, product questions, and loyalty
rewards through natural conversation.

You have access to the following tools — always prefer using them over
guessing or relying on general knowledge:

- Gateway tools (order tracking, refunds, account lookups): Use these to
  check order status, initiate or check on returns/refunds, and look up
  account details. Never invent order numbers, statuses, or refund outcomes.

- search_knowledge_base: Use this for product specifications, return and
  warranty policies, loyalty program rules, and other factual/support
  questions. Ground your answers in what this tool returns rather than
  assuming details about products or policies.

- calculate_loyalty_discount: Use this whenever a customer asks about
  loyalty discounts, points redemption, or an exact order total. Do not
  do this math yourself — always call the tool so the numbers are exact.

- browser: Use this to look up real-time information that isn't available
  through the knowledge base or gateway tools.

You also have long-term memory of this customer across sessions —
their name, preferences, and past conversations may be provided as
"Customer Context" before their message. Use that context naturally
(e.g. greet returning customers by name, remember stated preferences)
without explicitly mentioning that you have "memory" or "retrieved context."

Guidelines:
- Be concise, friendly, and professional.
- Ask clarifying questions (e.g. for an order number) when you need more
  information to use a tool correctly.
- Never fabricate order details, policy terms, or calculation results —
  always use the appropriate tool.
- If a tool fails or information truly isn't available, tell the customer
  honestly and offer to help another way.
"""

@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    try:
        # 1. Extract inputs from payload
        user_input = payload.get("prompt")
        actor_id = payload.get("customer_id")
        session_id = payload.get("session_id", str(uuid.uuid4())) 

        if not user_input:
            return {"error": "Missing required field: 'prompt'"}

        memory_hook = MemoryHook(actor_id=actor_id, 
                                session_id=session_id,
                                memory_id=MEMORY_ID,
                                memory_client=memory_client)

       
        # Playwright's driver binary is bundled under /var/task (read-only at runtime).
        # Copy it to /tmp (writable), fix its executable bit there, and redirect
        # Playwright to use that copy via its official env-var override.
        source_driver = "/var/task/playwright/driver/node"
        tmp_driver = "/tmp/playwright_driver_node"

        if os.path.exists(source_driver) and not os.path.exists(tmp_driver):
            shutil.copy2(source_driver, tmp_driver)
            os.chmod(tmp_driver, 0o755)
            logger.warning(f"Copied Playwright driver to {tmp_driver} with mode {oct(os.stat(tmp_driver).st_mode)}")

        os.environ["PLAYWRIGHT_NODEJS_PATH"] = tmp_driver

        agent_core_browser = AgentCoreBrowser(region=REGION,session_timeout=300)

        tools = [
            search_knowledge_base,
            calculate_loyalty_discount,
            agent_core_browser.browser,
        ]
        client = MCPClient(
        lambda: streamable_http_client(url=GATEWAY_URL)
	    )
        with client:
            gateway_tools = client.list_tools_sync()
            tools.extend(gateway_tools)
            agent = Agent(
                model=model,
                tools=tools,
                hooks=[memory_hook],
                state={"session_id": session_id, "actor_id": actor_id},
                system_prompt=SYSTEM_PROMPT,

            )
            response = await agent.invoke_async(user_input)
            return response.message["content"][0]["text"]

    except IndexError:
        logger.warning(f"Agent returned no text content for input: {user_input!r}")
        return "I wasn't able to complete that request. Could you try rephrasing, or ask something else?"

    except Exception as e:
        logging.error(f"Agent Invocation Failed: {e}")
        return {"error": f"Agent invocation failed: {str(e)}"}


# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()

    #print(f"Input: {args.payload}")
    response = asyncio.run(invoke(json.loads(args.payload)))

    print(f"\nOutput: {response}")

if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    # main()

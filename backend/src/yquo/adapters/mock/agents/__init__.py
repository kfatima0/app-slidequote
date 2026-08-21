"""The three fake supplier agents, one per channel. MOCK-PHASE ONLY.

`build_agents` is what composition calls: it returns the channel -> agent map the
orchestrator dispatches through, so adding a real agent later means changing this
one function rather than anything in app/.
"""

from yquo.adapters.mock.agents.base import MockSupplierAgent
from yquo.adapters.mock.agents.ecommerce import MockEcommerceAgent
from yquo.adapters.mock.agents.email import MockEmailAgent
from yquo.adapters.mock.agents.voice import MockVoiceAgent
from yquo.domain.enums import Channel
from yquo.ports.clock import Clock
from yquo.ports.repositories import ProductRepository
from yquo.ports.supplier_agent import SupplierAgent


def build_agents(products: ProductRepository, clock: Clock) -> dict[Channel, SupplierAgent]:
    return {
        Channel.ECOMMERCE: MockEcommerceAgent(products, clock),
        Channel.VOICE: MockVoiceAgent(products, clock),
        Channel.EMAIL: MockEmailAgent(products, clock),
    }


__all__ = [
    "MockEcommerceAgent",
    "MockEmailAgent",
    "MockSupplierAgent",
    "MockVoiceAgent",
    "build_agents",
]

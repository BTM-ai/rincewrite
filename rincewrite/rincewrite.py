"""Welcome to Reflex! This file outlines the steps to create a basic app."""

import os
from typing import Annotated, Any, AsyncGenerator
from typing_extensions import TypedDict
from langchain_core.pydantic_v1 import BaseModel, Field
from langgraph.graph import StateGraph
from langgraph.graph.message import add_messages
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain_openai import ChatOpenAI
from langchain import hub
from langchain_core.messages.base import BaseMessage
from langchain_core.runnables import RunnableConfig
import reflex as rx  # type: ignore
# Reflex does not provide type hints at the moment

model_name = "gpt-4o-mini"
model = ChatOpenAI(model=model_name, temperature=0.7, streaming=True)

SQLITE_CONN_STRING = "rincewrite.db"

piece_desc_placeholder = "Your piece description here. Any description that \
can help bootstrap the structuration of your piece is most welcome (title, \
chapters...). Anything about its contents is also welcome (subject, themes, \
characters, plot, ...). But don't waste too much time here: we will build \
this and the rest along the way, together."
user_desc_placeholder = "Your own description here. Any description that can \
help me bootstrap my behaviour towards you is most welcome (why do you write?\
, what do you like to write? ...). Anything about your character is also \
welcome (what are you trying to achieve by writing?, how do you like to be \
adressed? ...). But don't waste too much time here: we will build this and \
the rest along the way, together."


class PieceUpdate(BaseModel):
    new_title: str = Field(
        ...,
        title="New title for the piece")
    new_desc: str = Field(
        ...,
        title="New description for the piece")
    new_text: str = Field(
        ...,
        title="New text for the piece")


class GraphState(TypedDict):
    piece_title: str
    piece_desc: str
    piece_text: str
    piece_update: PieceUpdate
    messages: Annotated[list[BaseMessage], add_messages]


# 'welcome' Node
_welcome_prompt = hub.pull("btm-guirriecp/rincewrite-welcome:2fd2ab1f")
_welcome_chain = _welcome_prompt | model


async def _welcome(
    state: GraphState,
    config: RunnableConfig
) -> dict[str, Any]:

    welcome_msg = await _welcome_chain.ainvoke({
        "user_name":    config["configurable"].get(
            "user_name",
            "UNKNOWN_USER"),
        "user_desc":    config["configurable"].get(
            "user_desc",
            "NO_USER_DESC"),
        "piece_title":   state["piece_title"],
        "piece_desc":   state["piece_desc"],
    })

    return {"messages": [welcome_msg]}

# 'user_action' Node


def _user_action(state: GraphState) -> None:
    # this is a 'fake' node, serving as en entry point for the user's actions
    pass


# 'update_piece_text' Node
_update_piece_prompt = hub.pull(
    "btm-guirriecp/rincewrite-update_piece:8bf27135")
_update_piece_chain = _update_piece_prompt | model.with_structured_output(
    PieceUpdate)


async def _update_piece(
    state: GraphState,
    config: RunnableConfig
) -> dict[str, Any]:
    piece_update = await _update_piece_chain.ainvoke({
        "user_name":    config["configurable"].get(
            "user_name",
            "UNKNOWN_USER"),
        "user_desc":    config["configurable"].get(
            "user_desc",
            "NO_USER_DESC"),
        "piece_title":   state["piece_title"],
        "piece_desc":   state["piece_desc"],
        "piece_text":   state["piece_text"],
        "messages":     state["messages"],
    })

    return {
        "piece_update": piece_update}

# 'chat' Node
_chat_prompt = hub.pull("btm-guirriecp/rincewrite-chat:737df30f")
_chat_chain = _chat_prompt | model


async def _chat(
    state: GraphState,
    config: RunnableConfig
) -> dict[str, Any]:

    chat_msg = await _chat_chain.ainvoke({
        "user_name":    config["configurable"].get(
            "user_name",
            "UNKNOWN_USER"),
        "user_desc":    config["configurable"].get(
            "user_desc",
            "NO_USER_DESC"),
        "piece_title":   state["piece_title"],
        "piece_desc":   state["piece_desc"],
        "piece_text":   state["piece_text"],
        "messages":     state["messages"],
        "new_piece_text": state["piece_update"].new_text,
        "new_piece_title": state["piece_update"].new_title,
        "new_piece_desc": state["piece_update"].new_desc,
    })

    return {
        "piece_text": state["piece_update"].new_text,
        "piece_title": state["piece_update"].new_title,
        "piece_desc": state["piece_update"].new_desc,
        "messages": [chat_msg]}

graph_builder = StateGraph(GraphState)
graph_builder.add_node("welcome", _welcome)
graph_builder.add_node("user_action", _user_action)
graph_builder.add_node("update_piece", _update_piece)
graph_builder.add_node("chat", _chat)

graph_builder.set_entry_point("welcome")
graph_builder.add_edge("welcome", "user_action")
graph_builder.add_edge("user_action", "update_piece")
graph_builder.add_edge("update_piece", "chat")
graph_builder.add_edge("chat", "user_action")


class RWState(rx.State):  # type: ignore
    """The app state."""
    # intro dialog
    show_dialog: bool = True
    user_form_submitted: bool = False
    # main app col 1/3 : chat / workzone
    messages: list[dict[str, str]] = []
    service_button: str = "answer"  # proposed service will be situational
    # main app col 2/3 : action buttons
    buttons: list[str] = [  # proposed robot actions will be situational
        "i have no idea what i'm doing",
        "help me structure the thing",
        "i have a draft already"
    ]
    # main app col 3/3 : render zone
    renderer_content: str = ""

    # local storage state
    user_name: str = rx.LocalStorage()
    user_desc: str = rx.LocalStorage()
    piece_title: str = rx.LocalStorage()
    piece_desc: str = rx.LocalStorage()

    def handle_user_submit(self, data: dict[str, Any]) -> None:
        self.user_form_submitted = True

    async def welcome(
        self,
        data: dict[str, Any]
    ) -> AsyncGenerator[None, None]:
        self.show_dialog = False
        yield

        # stream LLM tokens
        self.messages.append({
            'type': "ai",
            'msg': "",
        })

        config = RunnableConfig({
            "configurable": {
                "thread_id": self.user_name,
                "user_name": self.user_name,
                "user_desc": self.user_desc}
        })
        async with AsyncSqliteSaver.from_conn_string(
                SQLITE_CONN_STRING) as memory:
            graph = graph_builder.compile(
                checkpointer=memory,
                interrupt_before=["user_action"]
            )

            # Displays the graph LangGraph if 'SHOW_GRAPH' is true
            # in the environment variable
            if os.getenv("SHOW_GRAPH") == "true":
                try:
                    from PIL import Image  # type: ignore
                    from io import BytesIO
                except ImportError:
                    raise ImportError(
                        "Could not import PIL python package. "
                        "Please install it with `poetry install --with dev`."
                    )
                img_data = graph.get_graph().draw_mermaid_png()
                img = Image.open(BytesIO(img_data))
                img.show()

            state_snapshot = await graph.aget_state(config)
            last_state = state_snapshot.values
            last_piece_text = ""
            if last_state:
                self.renderer_content = (
                    f"# {last_state['piece_title']}\n\n"
                    f"**{last_state['piece_desc']}**"
                    f"\n\n{last_state['piece_text']}"
                )
                last_piece_text = last_state["piece_text"]
            else:
                self.renderer_content = (
                    f"# {self.piece_title}\n\n"
                    f"**{self.piece_desc}**"
                )
            yield

            async for event in graph.astream_events(
                {
                    "piece_title":  self.piece_title,
                    "piece_desc":  self.piece_desc,
                    "piece_text":  last_piece_text,
                    "messages":    [],
                },
                config,
                version="v2"
            ):
                kind = event["event"]
                # emitted for each streamed token
                if kind == "on_chat_model_stream":
                    content = event["data"]["chunk"].content
                    # only display non-empty content (not tool calls)h
                    if content:
                        self.messages[-1]["msg"] += content
                        yield

    async def handle_user_msg_submit(
        self,
        data: dict[str, Any]
    ) -> AsyncGenerator[None, None]:
        self.messages.append({"type": "user", "msg": data["text_area_input"]})
        yield

        # manually update graph state with user message
        config = RunnableConfig({
            "configurable": {
                "thread_id": self.user_name,
                "user_name": self.user_name,
                "user_desc": self.user_desc}
        })
        async with AsyncSqliteSaver.from_conn_string(
                SQLITE_CONN_STRING) as memory:
            # found no other way to associate the graph with the memory than
            # through recompiling it
            graph = graph_builder.compile(
                checkpointer=memory,
                interrupt_before=["user_action"]
            )
            await graph.aupdate_state(
                config,
                {"messages": [data["text_area_input"]]},
                as_node="user_action")

        # resume graph execution and stream LLM tokens
        self.messages.append({
            'type': "ai",
            'msg': "",
        })
        async with AsyncSqliteSaver.from_conn_string(
                SQLITE_CONN_STRING) as memory:
            graph = graph_builder.compile(
                checkpointer=memory,
                interrupt_before=["user_action"]
            )
            async for event in graph.astream_events(
                None,
                config,
                version="v2"
            ):
                kind = event["event"]
                # emitted for each streamed token
                if kind == "on_chat_model_stream":
                    content = event["data"]["chunk"].content
                    # only display non-empty content (not tool calls)
                    if content:
                        self.messages[-1]["msg"] += content
                        yield
                if kind == "on_chat_model_end":
                    # using 'functions' / 'tools' / 'structured ouptut'
                    # creates an event with tool_calls
                    output = event["data"]["output"]
                    if output.tool_calls:
                        piece_update_dict = output.tool_calls[0]['args']
                        piece_update = PieceUpdate(**piece_update_dict)
                        self.set_piece_title(piece_update.new_title)
                        self.set_piece_desc(piece_update.new_desc)
                        self.renderer_content = (
                            f"# {piece_update.new_title}\n\n"
                            f"**{piece_update.new_desc}**\n\n"
                            f"{piece_update.new_text}"
                        )
                yield


def welcome_dialog() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.content(
            rx.vstack(
                rx.dialog.title(
                    rx.heading("Welcome to... me! I'm Rincewrite", size="5"),),
                rx.dialog.description(
                    rx.text(
                        "I will help you from start to finish with your piece \
                        of writing.",
                        align="center"),
                ),
                rx.cond(
                    ~RWState.user_form_submitted,
                    rx.form(
                        rx.vstack(
                            rx.text("If you would just tell me who you are.",
                                    align="center",
                                    color_scheme="blue",),
                            rx.input(
                                placeholder="Your own name here...",
                                name="user_name",
                                value=RWState.user_name,
                                on_change=RWState.set_user_name
                            ),
                            rx.text_area(
                                placeholder=user_desc_placeholder,
                                style={
                                    "& ::placeholder": {
                                        "text-align": "justify"
                                    },
                                },
                                rows="10",
                                width="100%",
                                name="user_desc",
                                value=RWState.user_desc,
                                on_change=RWState.set_user_desc
                            ),
                            rx.dialog.close(
                                rx.button("begin", type="submit"),),
                            spacing="3",
                            justify="center",
                            align="center",
                        ),
                        on_submit=RWState.handle_user_submit,
                        #  for some reason,
                        #  Reflex will serve the form data to
                        #  the alternative one ('piece' form)
                        #  if reset_on_submit is not set
                        reset_on_submit=True,
                    ),
                    rx.form(
                        rx.vstack(
                            rx.text("If you would just tell me which it is.",
                                    align="center",
                                    color_scheme="blue",),
                            rx.input(
                                placeholder="Your piece title here...",
                                name="piece_title",
                                value=RWState.piece_title,
                                on_change=RWState.set_piece_title
                            ),
                            rx.text_area(
                                placeholder=piece_desc_placeholder,
                                style={
                                    "& ::placeholder": {
                                        "text-align": "justify"
                                    },
                                },
                                rows="10",
                                width="100%",
                                name="piece_desc",
                                value=RWState.piece_desc,
                                on_change=RWState.set_piece_desc
                            ),
                            rx.button("truly begin now", type="submit"),
                            spacing="3",
                            justify="center",
                            align="center",
                        ),
                        on_submit=RWState.welcome,
                    ),
                ),
                rx.text(
                    "conjured ",
                    rx.code("@ Brest Social Engines"),
                ),
                rx.logo(),
                spacing="3",
                justify="center",
                align="center",
                min_height="50vh",
            ),
            # prevevent the dialog from closing in any other way than clicking
            # the 'begin' button
            on_escape_key_down=rx.prevent_default,
            on_interact_outside=rx.prevent_default,
        ),
        open=RWState.show_dialog,
    )


def chat_msg(msg: dict[str, str]) -> rx.Component:
    return rx.box(
        rx.markdown(
            msg["msg"],
            background=rx.cond(
                msg["type"] == "user",
                rx.color("mauve", 4),
                rx.color("accent", 4)
            ),
            color=rx.cond(
                msg["type"] == "user",
                rx.color("mauve", 12),
                rx.color("accent", 12),
            ),
            style={
                "display": "inline-block",
                "padding": "0.5em",
                "border_radius": "8px",
                "max_width": ["30em", "30em", "50em", "50em", "50em", "50em"],
            },
        ),
        align_self=rx.cond(
            msg["type"] == "user",
            "flex-end",
            "flex-start"
        ),
    )


def chat_messages() -> rx.Component:
    return rx.center(
        rx.vstack(
            rx.foreach(
                RWState.messages,
                chat_msg
            ),
            spacing="1",
            width="98%",
        ),
        width="100%",
    )


def draft_area() -> rx.Component:
    return rx.center(
        rx.vstack(
            rx.scroll_area(
                chat_messages(),
                type="always",
                scrollbars="vertical",
                width="95%",
                height="59%",
            ),
            rx.form(
                rx.vstack(
                    rx.text_area(
                        placeholder="Work from here...",
                        name="text_area_input",
                        width="100%",
                        height="100%",
                    ),
                    rx.button(
                        RWState.service_button,
                        type="submit",
                        color_scheme="blue",
                        width="40%",
                        style={"font_size": "14px"},
                    ),
                    spacing="2",
                    justify="center",
                    align="center",
                    height="100%",
                ),
                width="95%",
                height="39%",
                on_submit=RWState.handle_user_msg_submit,
                reset_on_submit=True,
            ),
            spacing="3",
            justify="center",
            align="center",
            width="100%",
            height="95%",
        ),
        width="100%",
        height="100%",
    )


def action_button(button: str) -> rx.Component:
    return rx.button(
        button,
        color_scheme="blue",
        width="90%",
        height="auto",
        style={"font_size": "14px"},
    )


def action_buttons() -> rx.Component:
    return rx.center(
        rx.vstack(
            rx.foreach(
                RWState.buttons,
                action_button,
            ),
            spacing="5",
            justify="center",
            align="center",
        ),
        width="100%",
        height="100%",
    ),


def app_content() -> rx.Component:
    return rx.flex(
        rx.box(
            draft_area(),
            width="45%",
        ),
        rx.box(
            action_buttons(),
            width="10%",
        ),
        rx.box(
            rx.center(
                rx.scroll_area(
                    rx.center(
                        rx.markdown(
                            RWState.renderer_content,
                            width="98%",
                        ),
                        width="100%",
                        height="100%",
                    ),
                    type="always",
                    scrollbars="vertical",
                    width="95%",
                    height="90%",
                ),
                width="100%",
                height="100%",
            ),
            width="45%",
            height="100%",
        ),
        width="100%",
        height="100%",
    )


def index() -> rx.Component:
    return rx.box(
        rx.color_mode.button(position="top-right"),
        welcome_dialog(),
        app_content(),
        width="100vw",
        height="100vh",
        overflow="hidden",
    )


app = rx.App()
app.add_page(index, title="Rincewrite")

"""ORM models. Importing this package registers every table on ``Base.metadata``, which is
what Alembic's autogenerate and the model-vs-migration test compare against."""

from personal_organizer.db.models.channel import ChannelInbox, ChannelOutbox

__all__ = ["ChannelInbox", "ChannelOutbox"]

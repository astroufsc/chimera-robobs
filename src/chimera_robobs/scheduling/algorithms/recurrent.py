# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Recurrent scheduling algorithm (id 3, name RECURRENT).

Targets are only scheduled if they were never observed or if they were
observed more than a specified number of days in the past.
"""

import datetime
import logging

from sqlalchemy import and_, or_

from chimera_robobs.scheduling.algorithms.base import (
    BaseScheduleAlgorith,
    RecurrentAlgorithException,
    get_session,
)
from chimera_robobs.scheduling.algorithms.higher import Higher
from chimera_robobs.scheduling.dates import datetime_from_jd
from chimera_robobs.scheduling.model import ObsBlock, RecurrentDB

log = logging.getLogger(__name__)


class Recurrent(BaseScheduleAlgorith):
    @staticmethod
    def name() -> str:
        return "RECURRENT"

    @staticmethod
    def id() -> int:
        return 3

    @staticmethod
    def process(*args, **kwargs):
        # Try to read the recurrence time from the configuration. If none is
        # provided, raise an exception.
        if ("config" not in kwargs) or ("recurrence" not in kwargs["config"]):
            raise RecurrentAlgorithException(
                "No configuration file provided or no recurrence time defined."
            )

        config = kwargs["config"]

        recurrence_time = config["recurrence"]

        slot_len = 1800.0
        if "slotLen" in kwargs:
            slot_len = kwargs["slotLen"]
        elif len(args) > 1:
            try:
                slot_len = float(args[0])
            except (TypeError, ValueError):
                slot_len = 1800.0
        elif "slotLen" in config:
            slot_len = config["slotLen"]

        # Filter targets by observing date. Leave "NeverObserved" and those
        # observed more than recurrence_time days ago.
        today = kwargs["site"].ut().replace(tzinfo=None)
        if "today" in kwargs:  # Needed for simulations...
            today = kwargs["today"].replace(tzinfo=None)
        reference_date = today - datetime.timedelta(days=recurrence_time)

        ntargets = len(kwargs["query"][:])
        # Exclude targets that were observed less than the specified amount
        # of time ago.
        kwargs["query"] = kwargs["query"].filter(
            or_(
                ObsBlock.observed == False,  # noqa: E712
                and_(
                    ObsBlock.observed == True,  # noqa: E712
                    ObsBlock.last_observation < reference_date,
                ),
            )
        )
        new_ntargets = len(kwargs["query"][:])
        log.debug("Filtering %i of %i targets", new_ntargets, ntargets)
        # Select targets with the Higher algorithm
        kwargs.pop("slotLen", None)
        return Higher.process(slotLen=slot_len, *args, **kwargs)

    @staticmethod
    def next(time, programs):
        """Select the program to observe with this scheduling algorithm."""
        return Higher.next(time, programs)

    @staticmethod
    def add(block):
        session = get_session()
        try:
            obsblock = session.merge(block[0])

            # Check if this is already in the database
            recurrent_block = (
                session.query(RecurrentDB)
                .filter(
                    RecurrentDB.pid == obsblock.pid,
                    RecurrentDB.block_id == obsblock.id,
                    RecurrentDB.tid == obsblock.target_id,
                )
                .first()
            )

            if recurrent_block is None:
                # Not in the database, add it
                recurrent_block = RecurrentDB()
                recurrent_block.pid = obsblock.pid
                recurrent_block.block_id = obsblock.id
                recurrent_block.tid = obsblock.target_id
                session.add(recurrent_block)
        finally:
            session.commit()

    @staticmethod
    def observed(time, program, site=None, soft=False):
        """Process program as observed."""
        obstime = datetime_from_jd(time + 2400000.5).replace(tzinfo=None)

        session = get_session()
        try:
            prog = session.merge(program[0])
            prog.finished = True
            obsblock = session.merge(program[2])
            obsblock.observed = True

            log.debug("%s: Marking as observed @ %s", obsblock.pid, obstime)

            if not soft:
                log.debug("Running in hard mode. Storing main information in database.")
                obsblock.last_observation = obstime
                recurrent_block = (
                    session.query(RecurrentDB)
                    .filter(
                        RecurrentDB.pid == obsblock.pid,
                        RecurrentDB.block_id == obsblock.id,
                        RecurrentDB.tid == obsblock.target_id,
                    )
                    .first()
                )
                if recurrent_block is None:
                    log.debug("Block not in recurrent database. Adding block...")
                    recurrent_block = RecurrentDB()
                    recurrent_block.pid = obsblock.pid
                    # legacy bug: a trailing comma stored a tuple here
                    recurrent_block.block_id = obsblock.id
                    recurrent_block.tid = obsblock.target_id
                    recurrent_block.visits = 1
                    recurrent_block.last_visit = obstime
                    session.add(recurrent_block)
                else:
                    recurrent_block.visits += 1
                    recurrent_block.last_visit = obstime

                    if 0 < recurrent_block.max_visits < recurrent_block.visits:
                        log.debug(
                            "Max visits (%i) reached. Marking as complete.",
                            recurrent_block.max_visits,
                        )
                        obsblock.completed = True
                    else:
                        log.debug("%i visits completed.", recurrent_block.visits)
            else:
                log.debug("Running in soft mode...")
        finally:
            session.commit()

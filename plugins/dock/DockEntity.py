# Copyright (c) 2014,2018 ADLINK Technology Inc.
#
# See the NOTICE file(s) distributed with this work for additional
# information regarding copyright ownership.
#
# This program and the accompanying materials are made available under the
# terms of the Eclipse Public License 2.0 which is available at
# http://www.eclipse.org/legal/epl-2.0, or the Apache License, Version 2.0
# which is available at https://www.apache.org/licenses/LICENSE-2.0.
#
# SPDX-License-Identifier: EPL-2.0 OR Apache-2.0
#
# Contributors: Gabriele Baldoni, ADLINK Technology Inc. - Base plugins set

import sys
import os
sys.path.append(os.path.join(sys.path[0], 'interfaces'))
from fog05.interfaces.States import State
from fog05.interfaces.Entity import Entity


class DockEntity(Entity):

    def __init__(self, uuid, name, image, ports_mappings):

        super(DockEntity, self).__init__()
        self.uuid = uuid
        self.name = name
        self.image_url = image
        self.ports_mappings = ports_mappings

    def on_configured(self, configuration):
        self.conf = configuration
        self.state = State.CONFIGURED

    def on_clean(self):
        self.state = State.DEFINED

    def on_start(self):
        self.state = State.RUNNING

    def on_stop(self):
        self.state = State.CONFIGURED

    def on_pause(self):
        self.state = State.PAUSED

    def on_resume(self):
        self.state = State.RUNNING

    def __str__(self):
        return "Name : {0} UUID: {1}".format(self.name, self.uuid)

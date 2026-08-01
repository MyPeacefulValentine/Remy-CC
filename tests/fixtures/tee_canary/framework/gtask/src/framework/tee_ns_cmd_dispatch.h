/*
 * Copyright (C) 2022 Huawei Technologies Co., Ltd.
 * Licensed under the Mulan PSL v2.
 * You can use this software according to the terms and conditions of the Mulan PSL v2.
 * You may obtain a copy of Mulan PSL v2 at:
 *     http://license.coscl.org.cn/MulanPSL2
 * THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY OR FIT FOR A PARTICULAR
 * PURPOSE.
 * See the Mulan PSL v2 for more details.
 */

#ifndef TEE_NS_CMD_DISPATCH_H
#define TEE_NS_CMD_DISPATCH_H

#include <stdint.h>
#include "ta_framework.h"

typedef TEE_Result (*sync_func)(const smc_cmd_t *cmd);

struct ns_sync_cmd_t {
    uint32_t cmd_id;
    sync_func func;
};

TEE_Result dispatch_ns_cmd(smc_cmd_t *cmd);

#endif

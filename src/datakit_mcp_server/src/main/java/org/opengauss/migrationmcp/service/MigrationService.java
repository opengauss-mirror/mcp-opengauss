package org.opengauss.migrationmcp.service;

import io.restassured.RestAssured;
import io.restassured.http.Header;
import io.restassured.response.Response;
import org.hamcrest.Matchers;
import org.opengauss.migrationmcp.entity.MigrationTask;
import org.opengauss.migrationmcp.entity.SourceDatabaseVo;
import org.opengauss.migrationmcp.entity.TargetDatabaseVo;
import org.opengauss.migrationmcp.utils.RestAssuredUtils;
import org.springframework.ai.tool.annotation.Tool;
import org.springframework.ai.tool.annotation.ToolParam;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import java.net.ConnectException;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

@Service
public class MigrationService {
    @Autowired
    private Header tokenHeader;

    @Autowired
    private PortalHostService portalHostService;

    @Autowired
    private DatabaseService databaseService;

    private final String basePath = "/plugins/data-migration/migration";

    @Tool(name = "启动数据迁移任务", description = "用于启动创建好的MySQL端到openGauss端的数据迁移任务")
    public void startMigrationTask(
            @ToolParam(required = true, description = "迁移任务名称") String taskName // 迁移任务名称
    ) {
        MigrationTask currentMigrationTask = null;

        List<MigrationTask> migrationTaskList = getMigrationTaskList();
        for (MigrationTask migrationTask : migrationTaskList) {
            if (migrationTask.getTaskName().equals(taskName)) {
                currentMigrationTask = migrationTask;
            }
        }

        if (currentMigrationTask == null) {
            throw new RuntimeException("无法查询到对应的迁移任务，请确保迁移任务存在");
        }

        RestAssuredUtils.setBasePath(basePath);
        Response response;
        try {
            response = RestAssured.given()
                    .header(tokenHeader)
                    .header("Content-Type", "application/json")
                    .pathParam("taskId", currentMigrationTask.getTaskId())
                    .when()
                    .post("/start/{taskId}");
        } catch (Exception e) {
            if (e instanceof ConnectException) {
                throw new RuntimeException("无法连接到DataKit服务，请检查服务是否正在运行");
            } else {
                throw new RuntimeException(e);
            }
        }

        try {
            response.then().body("code", Matchers.equalTo(200));
        } catch (AssertionError e) {
            throw new RuntimeException("启动数据迁移任务失败，失败原因：" + response.jsonPath().getString("data"));
        }
    }

    @Tool(name = "创建数据迁移任务", description = "用于创建MySQL端到openGauss端的数据迁移任务，方法返回任务名称")
    public String createMigrationTask(
            @ToolParam(required = true, description = "源端数据库所在主机IP") String sourceDatabaseIp,
            @ToolParam(required = true, description = "源端数据库的数据库端口") String sourceDatabasePort,
            @ToolParam(required = true, description = "源端数据库下的某个database") String sourceDatabaseName,
            @ToolParam(required = true, description = "目标端数据库所在主机ip") String targetDatabaseIp,
            @ToolParam(required = true, description = "目标端数据库的数据库端口") String targetDatabasePort,
            @ToolParam(required = true, description = "目标端数据库下的某个database") String targetDatabaseName,
            @ToolParam(required = false, description = "迁移任务名称，如果任务名称为空，则方法中会生成默认名称") String taskName
    ) {
        SourceDatabaseVo sourceDatabaseInfo = getSourceDatabaseInfo(sourceDatabaseIp, sourceDatabasePort, sourceDatabaseName);
        TargetDatabaseVo targetDatabaseInfo = getTargetDatabaseInfo(targetDatabaseIp, targetDatabasePort, targetDatabaseName);
        if (sourceDatabaseInfo == null || targetDatabaseInfo == null) {
            throw new RuntimeException("源端或目标端数据库不存在，请确保数据库存在");
        }

        HashMap<String, Object> task = new HashMap<>();
        task.put("isAdjustKernelParam", false);
        task.put("migrationModelId", 1);
        task.put("sourceDb", sourceDatabaseName);
        task.put("sourceDbHost", sourceDatabaseIp);
        task.put("sourceDbPort", sourceDatabasePort);
        task.put("sourceDbUser", sourceDatabaseInfo.getUsername());
        task.put("sourceDbPass", sourceDatabaseInfo.getPassword());
        task.put("sourceNodeId", sourceDatabaseInfo.getId());
        task.put("targetDb", targetDatabaseName);
        task.put("targetDbHost", targetDatabaseIp);
        task.put("targetDbPort", targetDatabasePort);
        task.put("targetDbUser", targetDatabaseInfo.getUsername());
        task.put("targetDbPass", targetDatabaseInfo.getPassword());
        task.put("targetDbVersion", null);
        task.put("targetNodeId", targetDatabaseInfo.getId());
        task.put("isSystemAdmin", true);
        task.put("sourceTables", "");
        task.put("taskParams", Collections.emptyList());

        List<String> hostIds = portalHostService.getPortalHostIds();
        if (taskName == null || taskName.isBlank()) {
            taskName = String.format("task_%s_to_%s", sourceDatabaseName, targetDatabaseName);
        }

        Map<String, Object> requestBody = new HashMap<>();
        requestBody.put("taskName", taskName);
        requestBody.put("globalParams", Collections.emptyList());
        requestBody.put("hostIds", hostIds);
        requestBody.put("tasks", List.of(task));

        RestAssuredUtils.setBasePath(basePath);
        Response response;
        try {
            response = RestAssured.given()
                    .header(tokenHeader)
                    .header("Content-Type", "application/json")
                    .body(requestBody)
                    .when()
                    .post("/save");
        } catch (Exception e) {
            if (e instanceof ConnectException) {
                throw new RuntimeException("无法连接到DataKit服务，请检查服务是否正在运行");
            } else {
                throw new RuntimeException(e);
            }
        }

        try {
            response.then().body("code", Matchers.equalTo(200));
            return taskName;
        } catch (AssertionError e) {
            throw new RuntimeException("创建数据迁移任务失败，失败原因：" + response.jsonPath().getString("data"));
        }
    }

    @Tool(name = "数据迁移任务列表", description = "用于获取已经创建好的数据迁移任务列表信息")
    public List<MigrationTask> getMigrationTaskList() {
        RestAssuredUtils.setBasePath(basePath);

        Response response;
        try {
            response = RestAssured.given()
                    .header(tokenHeader)
                    .when()
                    .get("/list?pageNum=1&pageSize=100");
        } catch (Exception e) {
            if (e instanceof ConnectException) {
                throw new RuntimeException("无法连接到DataKit服务，请检查服务是否正在运行");
            } else {
                throw new RuntimeException(e);
            }
        }

        response.then().body("code", Matchers.equalTo(200));
        List<MigrationTask> migrationTaskList = response.jsonPath().getList("rows", MigrationTask.class);

        for (MigrationTask migrationTask : migrationTaskList) {
            String taskId = migrationTask.getTaskId();

            Response res;
            try {
                res = RestAssured.given()
                        .header(tokenHeader)
                        .pathParam("taskId", taskId)
                        .when()
                        .get("/subTasks/{taskId}?pageNum=1&pageSize=100");
            } catch (Exception e) {
                if (e instanceof ConnectException) {
                    throw new RuntimeException("无法连接到DataKit服务，请检查服务是否正在运行");
                } else {
                    throw new RuntimeException(e);
                }
            }

            res.then().body("code", Matchers.equalTo(200));

            List<MigrationTask> rows = res.jsonPath().getList("rows", MigrationTask.class);
            if (rows != null && !rows.isEmpty()) {
                MigrationTask row = rows.getFirst();
                migrationTask.setSourceDatabaseIp(row.getSourceDatabaseIp());
                migrationTask.setSourceDatabasePort(row.getSourceDatabasePort());
                migrationTask.setSourceDatabaseName(row.getSourceDatabaseName());
                migrationTask.setTargetDatabaseIp(row.getTargetDatabaseIp());
                migrationTask.setTargetDatabasePort(row.getTargetDatabasePort());
                migrationTask.setTargetDatabaseName(row.getTargetDatabaseName());
            }
        }

        return migrationTaskList;
    }

    private SourceDatabaseVo getSourceDatabaseInfo(
            String sourceDatabaseIp, String sourceDatabasePort, String sourceDatabaseName) {
        List<String> databaseList = databaseService.getSourceDatabaseNameList(sourceDatabaseIp, sourceDatabasePort);
        if (databaseList == null || !databaseList.contains(sourceDatabaseName)) {
            // 无效的源端数据库信息
            return null;
        }

        List<SourceDatabaseVo> sourceDatabaseVos = databaseService.doGetSourceDatabaseList();
        for (SourceDatabaseVo sourceDatabaseVo : sourceDatabaseVos) {
            if (sourceDatabaseVo.getIp().equals(sourceDatabaseIp)
                    && sourceDatabaseVo.getPort().equals(sourceDatabasePort)) {
                return sourceDatabaseVo;
            }
        }
        return null;
    }

    private TargetDatabaseVo getTargetDatabaseInfo(
            String targetDatabaseIp, String targetDatabasePort, String targetDatabaseName) {
        List<String> databaseNameList = databaseService.getTargetDatabaseNameList(targetDatabaseIp, targetDatabasePort);
        if (databaseNameList == null || databaseNameList.isEmpty() || !databaseNameList.contains(targetDatabaseName)) {
            // 无效的目标端数据库信息
            return null;
        }

        List<TargetDatabaseVo> targetDatabaseVos = databaseService.doGetTargetDatabaseList();
        for (TargetDatabaseVo targetDatabaseVo : targetDatabaseVos) {
            if (targetDatabaseVo.getIp().equals(targetDatabaseIp)
                    && targetDatabaseVo.getPort().equals(targetDatabasePort)) {
                return targetDatabaseVo;
            }
        }
        return null;
    }
}
